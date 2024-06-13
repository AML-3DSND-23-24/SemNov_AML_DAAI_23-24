import sys
import os
import warnings
import numpy as np

sys.path.append(os.getcwd())
import os.path as osp
import time
import torch
from torch.cuda.amp import GradScaler, autocast
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.utils.data.distributed import DistributedSampler
from torch.utils.data import DataLoader
from torchvision import transforms
from utils.utils import *
from utils.dist import *
# noinspection PyUnresolvedReferences
from utils.data_utils import H5_Dataset
from datasets.modelnet import *
from datasets.scanobject import *
from models.classifiers import Classifier
from utils.ood_utils import get_confidence, eval_ood_sncore, iterate_data_odin, \
    iterate_data_energy, iterate_data_gradnorm, iterate_data_react, estimate_react_thres, print_ood_output, \
    get_penultimate_feats, get_network_output
import wandb
from base_args import add_base_args
from sklearn.metrics import accuracy_score, balanced_accuracy_score
from models.common import convert_model_state, logits_entropy_loss
from models.ARPL_utils import Generator, Discriminator
from classifiers.common import train_epoch_cla, train_epoch_rsmix_exposure, train_epoch_cs

# COPILOT MADE THE TRICK: 
#  clone the openshape-demo-support in the same directory of 3dos
#  run pip install .
#  import will not work: hover with mouse and click on the lightbulb to fix by adding the path to the sys.path
#import openshape 


def get_args():
    parser = argparse.ArgumentParser("OOD on point clouds via contrastive learning")
    parser = add_base_args(parser)

    # experiment specific arguments
    parser.add_argument("--augm_set",
                        type=str, default="rw", help="data augmentation choice", choices=["st", "rw"])
    parser.add_argument("--grad_norm_clip",
                        default=-1, type=float, help="gradient clipping")
    parser.add_argument("--num_points",
                        default=1024, type=int, help="number of points sampled for each object view")
    parser.add_argument("--num_points_test",
                        default=2048, type=int, help="number of points sampled for each SONN object - only for testing")
    parser.add_argument("--wandb_name", type=str, default=None)
    parser.add_argument("--wandb_group", type=str, default="md-2-sonn-augmCorr")
    parser.add_argument("--wandb_proj", type=str, default="benchmark-3d-ood-cla")
    parser.add_argument("--loss", type=str, default="CE",
                        choices=["CE", "CE_ls", "cosface", "arcface", "subcenter_arcface", "ARPL", "cosine"],
                        help="Which loss to use for training. CE is default")
    parser.add_argument("--cs", action='store_true', help="Enable confusing samples for ARPL")
    parser.add_argument("--cs_gan_lr", type=float, default=0.0002, help="Confusing samples GAN lr")
    parser.add_argument("--cs_beta", type=float, default=0.1, help="Beta loss weight for CS")
    parser.add_argument("--save_feats", type=str, default=None, help="Path where to save feats of penultimate layer")

    # Adopt Corrupted data
    # this flag should be set also during evaluation if testing Synth->Real Corr/LIDAR Augmented models
    parser.add_argument("--corruption",
                        type=str, default=None, help="type of corrupted data (lidar,occlusion,all) - default is None")
    args = parser.parse_args()

    #args.data_root = os.path.expanduser(args.data_root)
    args.data_root = osp.abspath("3D_OS_release_data")
    assert os.path.exists(args.data_root)
    print(f"Data root: {args.data_root}")
    args.tar1 = "none"
    args.tar2 = "none"

    if args.script_mode == 'eval':
        args.batch_size = 1

    return args


### data mgmt ###

def get_list_corr_data(opt, severity=None, split="train"):
    assert split in ['train', 'test']

    if opt.src == "SR1":
        prefix = "modelnet_set1"
    elif opt.src == "SR2":
        prefix = "modelnet_set2"
    else:
        raise ValueError(f"Expected SR source but received: {opt.src} ")

    print(f"get_list_corr_data for {prefix} - split {split}")

    # loads corrupted data
    if severity is None:
        severity = [1, 2, 3, 4]
    if opt.corruption == 'lidar' or opt.corruption == 'occlusion':
        print(f"loading {opt.corruption} data")
        root = osp.join(opt.data_root, "ModelNet40_corrupted", opt.corruption)
        file_names = [f"{root}/{prefix}_{split}_{opt.corruption}_sev" + str(i) + ".h5" for i in severity]
        print(f"corr list files: {file_names}\n")
    elif opt.corruption == 'all':
        print("loading both lidar and occlusion data")
        file_names = []
        root_lidar = osp.join(opt.data_root, "ModelNet40_corrupted", "lidar")
        file_names.extend([f"{root_lidar}/{prefix}_{split}_lidar_sev" + str(i) + ".h5" for i in severity])
        root_occ = osp.join(opt.data_root, "ModelNet40_corrupted", "occlusion")
        file_names.extend([f"{root_occ}/{prefix}_{split}_occlusion_sev" + str(i) + ".h5" for i in severity])
        print(f"corr list files: {file_names}\n")
    else:
        raise ValueError(f"Unknown corruption specified: {opt.corruption}")

    # augmentation mgmt
    if opt.script_mode.startswith("eval"):
        augm_set = None
    else:
        # synth -> real augm
        warnings.warn(f"Using RW augmentation set for corrupted data")
        augm_set = transforms.Compose([
            PointcloudToTensor(),
            AugmScale(),
            AugmRotate(axis=[0.0, 1.0, 0.0]),
            AugmRotatePerturbation(),
            AugmTranslate(),
            AugmJitter()
        ])

    corrupted_datasets = []
    for h5_path in file_names:
        corrupted_datasets.append(H5_Dataset(h5_file=h5_path, num_points=opt.num_points, transforms=augm_set))

    return corrupted_datasets


### for evaluation routine ###
def get_md_eval_loaders(opt):
    assert opt.script_mode.startswith("eval")
    if not str(opt.src).startswith('SR'):
        raise ValueError(f"Unknown modelnet src: {opt.src}")

    train_data = ModelNet40_OOD(
        data_root=opt.data_root,
        train=True,
        num_points=opt.num_points,
        class_choice=opt.src,
        transforms=None)

    print(f"{opt.src} train data len: {len(train_data)}")

    # append corrupted data to train dataset
    if opt.corruption:
        l_corr_data = get_list_corr_data(opt)  # list of corrupted datasets
        assert isinstance(l_corr_data, list)
        assert isinstance(l_corr_data[0], data.Dataset)
        l_corr_data.append(train_data)  # appending clean data to list corrupted datasets
        train_data = torch.utils.data.ConcatDataset(l_corr_data)  # concat Dataset
        print(f"Cumulative (clean+corrupted) train data len: {len(train_data)}")

    # test data (only clean samples)
    test_data = ModelNet40_OOD(
        data_root=opt.data_root,
        train=False,
        num_points=opt.num_points,
        class_choice=opt.src,
        transforms=None)

    train_loader = DataLoader(train_data, batch_size=opt.batch_size, num_workers=opt.num_workers,
                              worker_init_fn=init_np_seed, shuffle=False, drop_last=False)
    test_loader = DataLoader(test_data, batch_size=opt.batch_size, num_workers=opt.num_workers,
                             worker_init_fn=init_np_seed, shuffle=False, drop_last=False)
    return train_loader, test_loader

##############################


def eval_ood_md2sonn(opt, config):
    print(f"Arguments: {opt}")
    set_random_seed(opt.seed)

    dataloader_config = {
        'batch_size': opt.batch_size, 'drop_last': False, 'shuffle': False,
        'num_workers': opt.num_workers, 'sampler': None, 'worker_init_fn': init_np_seed}

    # whole evaluation is done on ScanObject RW data
    sonn_args = {
        'data_root': opt.data_root,
        'sonn_split': opt.sonn_split,
        'h5_file': opt.sonn_h5_name,
        'split': 'all',  # we use both training (unused) and test samples during evaluation
        'num_points': opt.num_points_test,  # default: use all 2048 sonn points to avoid sampling randomicity
        'transforms': None  # no augmentation applied at inference time
    }

    train_loader, _ = get_md_eval_loaders(opt)
    if opt.src == 'SR1':
        print("Src is SR1\n")
        id_loader = DataLoader(ScanObject(class_choice="sonn_2_mdSet1", **sonn_args), **dataloader_config)
        ood1_loader = DataLoader(ScanObject(class_choice="sonn_2_mdSet2", **sonn_args), **dataloader_config)
    elif opt.src == 'SR2':
        print("Src is SR2\n")
        id_loader = DataLoader(ScanObject(class_choice="sonn_2_mdSet2", **sonn_args), **dataloader_config)
        ood1_loader = DataLoader(ScanObject(class_choice="sonn_2_mdSet1", **sonn_args), **dataloader_config)
    else:
        raise ValueError(f"OOD evaluation - wrong src: {opt.src}")

    # second SONN out-of-distribution set is common to both SR1 and SR2 sources
    # these are the samples from SONN categories with poor mapping to ModelNet categories
    ood2_loader = DataLoader(ScanObject(class_choice="sonn_ood_common", **sonn_args), **dataloader_config)

    
    classes_dict = eval(opt.src)
    n_classes = len(set(classes_dict.values()))
    model = Classifier(args=DotConfig(config['model']), num_classes=n_classes, loss=opt.loss, cs=opt.cs)
    model = model.cuda().eval() # Duplicated
    #model.eval
    """
    ckt_weights = torch.load(opt.ckpt_path, map_location='cpu')['model']
    ckt_weights = sanitize_model_dict(ckt_weights)
    ckt_weights = convert_model_state(ckt_weights, model.state_dict())
    print(f"Model params count: {count_parameters(model) / 1000000 :.4f} M")
    print("Load weights: ", model.load_state_dict(ckt_weights, strict=True))
    model = model.cuda().eval()

    src_logits, src_pred, src_labels = get_network_output(model, id_loader)
    tar1_logits, tar1_pred, tar1_labels = get_network_output(model, ood1_loader)
    tar2_logits, tar2_pred, tar2_labels = get_network_output(model, ood2_loader)

    # MSP
    print("\n" + "#" * 80)
    print("Computing OOD metrics with MSP normality score...")
    src_MSP_scores = F.softmax(src_logits, dim=1).max(1)[0]
    tar1_MSP_scores = F.softmax(tar1_logits, dim=1).max(1)[0]
    tar2_MSP_scores = F.softmax(tar2_logits, dim=1).max(1)[0]
    eval_ood_sncore(
        scores_list=[src_MSP_scores, tar1_MSP_scores, tar2_MSP_scores],
        preds_list=[src_pred, tar1_pred, tar2_pred],  # computes also MSP accuracy on ID test set
        labels_list=[src_labels, tar1_labels, tar2_labels],  # computes also MSP accuracy on ID test set
        src_label=1,
        src = opt.src)
    print("#" * 80)

    # MLS
    print("\n" + "#" * 80)
    print("Computing OOD metrics with MLS normality score...")
    src_MLS_scores = src_logits.max(1)[0]
    tar1_MLS_scores = tar1_logits.max(1)[0]
    tar2_MLS_scores = tar2_logits.max(1)[0]
    eval_ood_sncore(
        scores_list=[src_MLS_scores, tar1_MLS_scores, tar2_MLS_scores],
        preds_list=[src_pred, tar1_pred, tar2_pred],  # computes also MSP accuracy on ID test set
        labels_list=[src_labels, tar1_labels, tar2_labels],  # computes also MSP accuracy on ID test set
        src_label=1,
        src = opt.src)
    print("#" * 80)

    # entropy
    print("\n" + "#" * 80)
    src_entropy_scores = 1 / logits_entropy_loss(src_logits)
    tar1_entropy_scores = 1 / logits_entropy_loss(tar1_logits)
    tar2_entropy_scores = 1 / logits_entropy_loss(tar2_logits)
    print("Computing OOD metrics with entropy normality score...")
    eval_ood_sncore(
        scores_list=[src_entropy_scores, tar1_entropy_scores, tar2_entropy_scores],
        preds_list=[src_pred, tar1_pred, tar2_pred],  # computes also MSP accuracy on ID test set
        labels_list=[src_labels, tar1_labels, tar2_labels],  # computes also MSP accuracy on ID test set
        src_label=1,
        src = opt.src)
    print("#" * 80)

    """

    # FEATURES EVALUATION
    eval_OOD_with_feats(model, train_loader, id_loader, ood1_loader, ood2_loader, save_feats=opt.save_feats, src=opt.src)

def L_k_norm(features1, features2, k):
    return torch.sum(torch.abs(features1 - features2) ** k, dim=-1) ** (1 / k)

def knn_custom(train_feats, src_feats, k, norm_k):
    num_src = src_feats.size(0)
    num_train = train_feats.size(0)
    
    distances = torch.zeros((num_src, num_train))
    
    for i in range(num_src):
        distances[i] = L_k_norm(train_feats, src_feats[i].unsqueeze(0).repeat(num_train, 1), norm_k)
    
    # Trova i k-nearest neighbors
    src_dist, src_ids = torch.topk(distances, k, dim=1, largest=False)
    
    return src_dist, src_ids

def eval_OOD_with_feats(model, train_loader, src_loader, tar1_loader, tar2_loader, save_feats=None, src="_"):
    from knn_cuda import KNN
    knn = KNN(k=1, transpose_mode=True)

    print("\n" + "#" * 80)
    print("Computing OOD metrics with distance from train features...")

    #STEP 3 PATH:
    # extract penultimate features, compute distances
    # train_feats, train_labels = get_penultimate_feats(model, train_loader)
    # src_feats, src_labels = get_penultimate_feats(model, src_loader)
    # tar1_feats, tar1_labels = get_penultimate_feats(model, tar1_loader)
    # tar2_feats, tar2_labels = get_penultimate_feats(model, tar2_loader)

    #STEP 4 PATH:
    
    #pc_encoder(torch.tensor(pc[:, [0, 2, 1, 3, 4, 5]].T[None], device=ref_dev)).cpu()
    #train_feats, train_labels = [ pc_encoder(torch.tensor(batch[0][:, [0, 2, 1, 3, 4, 5]].T[None],device=next(pc_encoder.parameters()).device)).cpu(), batch[1] for batch in train_loader]
    
    
    src_feats, src_labels = get_penultimate_feats(model, src_loader)
    train_feats, train_labels = get_penultimate_feats(model, train_loader)


    tar1_feats, tar1_labels = get_penultimate_feats(model, tar1_loader)
    tar2_feats, tar2_labels = get_penultimate_feats(model, tar2_loader)
    train_labels = train_labels.cpu().numpy()

    # labels_set = set(train_labels)
    # prototypes = torch.zeros((len(labels_set), train_feats.shape[1]), device=train_feats.device)
    # for idx, lbl in enumerate(labels_set):
    #     mask = train_labels == lbl
    #     prototype = train_feats[mask].mean(0)
    #     prototypes[idx] = prototype

    # if save_feats is not None:
    #     if isinstance(train_loader.dataset, ModelNet40_OOD):
    #         labels_2_names = {v: k for k, v in train_loader.dataset.class_choice.items()}
    #     else:
    #         labels_2_names = {}

    #     output_dict = {}
    #     output_dict["labels_2_names"] = labels_2_names
    #     output_dict["train_feats"], output_dict["train_labels"] = train_feats.cpu(), train_labels
    #     output_dict["id_data_feats"], output_dict["id_data_labels"] = src_feats.cpu(), src_labels
    #     output_dict["ood1_data_feats"], output_dict["ood1_data_labels"] = tar1_feats.cpu(), tar1_labels
    #     output_dict["ood2_data_feats"], output_dict["ood2_data_labels"] = tar2_feats.cpu(), tar2_labels
    #     torch.save(output_dict, save_feats)
    #     print(f"Features saved to {save_feats}")

    ################################################

    
    print("Euclidean distances in a non-normalized space:")
    # eucl distance in a non-normalized space
    
    # Calcola i k-NN utilizzando la distanza L0.1
    norm_k = 1
    k = 1  # Numero di vicini da considerare, in questo caso 1 per nearest neighbor
    
    train_feats = train_feats.cuda()
    
    src_feats = src_feats.cuda()
    src_dist, src_ids = knn_custom(train_feats, src_feats, k, norm_k)
    
    # Porta i risultati in CPU
    src_dist = src_dist.squeeze().cpu()
    src_ids = src_ids.squeeze().cpu()  # Indici dei nearest training samples
    src_scores = 1 / src_dist  # Punteggi inversamente proporzionali alla distanza
    src_pred = np.asarray([train_labels[i] for i in src_ids])  # Predizione basata sull'etichetta del nearest training sample

    # OOD tar1
    tar1_feats = tar1_feats.cuda()
    tar1_dist, tar1_ids = knn_custom(train_feats, tar1_feats, k, norm_k)

    # Porta i risultati in CPU
    tar1_dist = tar1_dist.squeeze().cpu()
    tar1_ids = tar1_ids.squeeze().cpu()  # Indici dei nearest training samples
    tar1_scores = 1 / tar1_dist  # Punteggi inversamente proporzionali alla distanza
    tar1_pred = np.asarray([train_labels[i] for i in tar1_ids])  # Predizione basata sull'etichetta del nearest training sample

    # OOD tar2
    tar2_feats = tar2_feats.cuda()
    tar2_dist, tar2_ids = knn_custom(train_feats, tar2_feats, k, norm_k)

    # Porta i risultati in CPU
    tar2_dist = tar2_dist.squeeze().cpu()
    tar2_ids = tar2_ids.squeeze().cpu()  # Indici dei nearest training samples
    tar2_scores = 1 / tar2_dist  # Punteggi inversamente proporzionali alla distanza
    tar2_pred = np.asarray([train_labels[i] for i in tar2_ids])  # Predizione basata sull'etichetta del nearest training sample

    eval_ood_sncore(
         scores_list=[src_scores, tar1_scores, tar2_scores],
         preds_list=[src_pred, tar1_pred, tar2_pred],  # [src_pred, None, None],
         labels_list=[src_labels, tar1_labels, tar2_labels],  # [src_labels, None, None],
         src_label=1,  # confidence should be higher for ID samples
         src=src
    )

    # eval_ood_sncore(
    #     scores_list=[src_scores, tar1_scores, tar2_scores],
    #     preds_list=[src_pred, tar1_pred, tar2_pred],  # [src_pred, None, None],
    #     labels_list=[src_labels, tar1_labels, tar2_labels],  # [src_labels, None, None],
    #     src_label=1  # confidence should be higher for ID samples
    # )

    """

    print("#" * 80)

    ################################################
    print("\nCosine similarities on the hypersphere:")
    # cosine sim in a normalized space
    train_feats = F.normalize(train_feats, p=2, dim=1)
    src_feats = F.normalize(src_feats, p=2, dim=1)
    tar1_feats = F.normalize(tar1_feats, p=2, dim=1)
    tar2_feats = F.normalize(tar2_feats, p=2, dim=1)
    src_scores, src_ids = torch.mm(src_feats, train_feats.t()).max(1)
    tar1_scores, tar1_ids = torch.mm(tar1_feats, train_feats.t()).max(1)
    tar2_scores, tar2_ids = torch.mm(tar2_feats, train_feats.t()).max(1)
    src_pred = np.asarray([train_labels[i] for i in src_ids])  # pred is label of nearest training sample
    tar1_pred = np.asarray([train_labels[i] for i in tar1_ids])  # pred is label of nearest training sample
    tar2_pred = np.asarray([train_labels[i] for i in tar2_ids])  # pred is label of nearest training sample

    eval_ood_sncore(
        scores_list=[(0.5 * src_scores + 0.5).cpu(), (0.5 * tar1_scores + 0.5).cpu(), (0.5 * tar2_scores + 0.5).cpu()],
        preds_list=[src_pred, tar1_pred, tar2_pred],  # [src_pred, None, None],
        labels_list=[src_labels, tar1_labels, tar2_labels],  # [src_labels, None, None],
        src_label=1,  # confidence should be higher for ID samples
        src = src
    )
    """

def main():
    args = get_args()
    config = load_yaml(args.config)

    if args.script_mode.startswith('train'):
        # launch trainer
        print("training...")
        assert args.checkpoints_dir is not None and len(args.checkpoints_dir)
        assert args.exp_name is not None and len(args.exp_name)
        args.log_dir = osp.join(args.checkpoints_dir, args.exp_name)
        args.tb_dir = osp.join(args.checkpoints_dir, args.exp_name, "tb-logs")
        args.models_dir = osp.join(args.checkpoints_dir, args.exp_name, "models")
        args.backup_dir = osp.join(args.checkpoints_dir, args.exp_name, "backup-code")
        #train(args, config)
    else:
        # eval Modelnet -> SONN
        # assert args.ckpt_path is not None and len(args.ckpt_path)
        print("out-of-distribution eval - Modelnet -> SONN ..")
        eval_ood_md2sonn(args, config)

if __name__ == '__main__':
    main()
