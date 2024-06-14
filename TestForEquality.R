# we fix the seed for reproducibility 
set.seed(1234)
# we load the two 3D point clouds
df1 = read.csv('chair_0001.txt',sep=',',header=FALSE)
df2 = read.csv('sofa_0001.txt',sep=',',header=FALSE)
# some libraries to help us 
require(np)
require(HAC)
## let's remove the RGB channels from the points
sample.A <- df1[,1:3]
sample.B <- df2[,1:3]
# let's simultaneously compute the empirical copulas and extract two samples z1 and z2
z1 = emp.copula.self(as.matrix(sample.A), proc = "M") 
z2 = emp.copula.self(as.matrix(sample.B), proc = "M") 
# let's check for the equality in the dependency structure
npunitest(z1,z2,boot.num=99)
