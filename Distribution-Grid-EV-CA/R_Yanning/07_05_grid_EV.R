library(data.table)

load.PGE <- fread('data/grid data/clean/clean load PGE_full hour.csv')
load.SCE <- fread('data/grid data/clean/clean load SCE_fixed.csv')
load.SDGE <- fread('data/grid data/clean/clean load SDGE_2022.csv')


cap.PGE <- fread('data/grid data/clean/clean ICA PGE_all.csv')
cap.SCE <- fread('data/grid data/clean/clean GNA SCE.csv')
cap.SDGE <- fread('data/grid data/clean/clean ICA SDGE_all.csv')

EV <- readRDS('data/result/charging profile by feeder/profile_feeder_cum_bins_IOU_sample42_draw12_split6699_sample36.rds')
head(EV)
summary(EV)

setnames(cap.PGE,"NetworkId","FeederID")
setnames(cap.PGE,"Month","month")
setnames(cap.PGE,"Hour","hour")
setnames(cap.PGE,"capacity_kW","headroom")
PGE <- merge(x=load.PGE[,c("FeederID","load_high",'month',"hour")],y=cap.PGE,
             by=c("FeederID","month","hour"))
setnames(PGE,"load_high","baseload")
PGE[,capacity:=headroom+baseload]
PGE[,FeederID:=as.character(FeederID)]
unique(nchar(PGE$FeederID))
PGE[nchar(FeederID)==8,FeederID:=paste0('0',FeederID)]
PGE[,utility:='PGE']
summary(PGE)
PGE[capacity<0]

SCE <- merge(x=load.SCE,y=cap.SCE[,c('FeederID',"capacity_kW")],
             by='FeederID')
setnames(SCE,"load_high","baseload")
setnames(SCE,"capacity_kW","capacity")
SCE[,headroom:=capacity-baseload]
SCE[,utility:='SCE']
summary(SCE)
SCE[headroom<0]

unique(nchar(load.SDGE$FeederID))
cap.SDGE[,FeederID:=as.character(FeederID)]
SDGE <- merge(x=load.SDGE,y=cap.SDGE[,c("FeederID",'month','hour','capacity')],
              by=c("FeederID","month","hour"))
setnames(SDGE,'capacity','headroom')
setnames(SDGE,'load','baseload')
SDGE[,capacity:=headroom+baseload]
SDGE[,utility:='SDGE']
summary(SDGE)
SDGE[capacity<0]
SDGE[capacity>16000]

grid <- rbind(PGE,SCE,SDGE)

EV.sum <- EV[,.(EVload=sum(load)),by=.(FeederID,year,hour)]
EV.sum <- EV.sum[rep(1:nrow(EV.sum),each=12),]
EV.sum[,month:=rep(1:12,times=nrow(EV.sum)/12)]
head(EV.sum)

EV.grid <- merge(x=EV.sum,y=grid,
                 by=c('FeederID','month','hour'))
head(EV.grid)
length(unique(EV.grid$FeederID)) # 5582 valid feeders in total
EV.grid[,overload:=EVload-headroom]
nrow(EV.grid[year==2022&overload>0]) # 34557 hours
nrow(EV.grid[year==2045&overload>0]) # 496593 hours
length(unique(EV.grid[year==2022&headroom<=0]$FeederID)) # 654/5582 (data issues)
length(unique(EV.grid[year==2022&overload>0]$FeederID)) # 795/5582
length(unique(EV.grid[year==2045&overload>0]$FeederID)) # 4452/5582

saveRDS(EV.grid,'data/result/overload/overload_sample42_draw12_split6699_sample36.rds')
