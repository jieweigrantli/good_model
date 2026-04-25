library(data.table)
library(tidyverse)

event.sd <- readRDS('data/mobility data/CSTDM processed/charge_event_new_SDPTM_sample42_draw12.rds')
event.ld <- readRDS('data/mobility data/CSTDM processed/charge_event_new_LDPTM_sample42.rds')
event.etm <- readRDS('data/mobility data/CSTDM processed/charge_event_new_ETM_sample42.rds')

setnames(event.sd,"chargeDist","dist")
setnames(event.sd,"ChargeType","type")
setnames(event.sd,"J","TAZ")
setnames(event.sd,"Time","time")
event.sd <- event.sd[,c("year","TAZ","dist","type","time")]
event.sd[,trip:="sd"]

setnames(event.ld,"TAZway","TAZ")
setnames(event.ld,"chargeDist","dist")
setnames(event.ld,"ChargeEventType","type")
setnames(event.ld,"Time","time")
event.ld[other=='corridor',trip:='ld_corridor']
event.ld[other=='destination',trip:='ld_destination']
event.ld <- event.ld[,c("year","TAZ","dist","type","time","trip")]

setnames(event.etm,"TAZway","TAZ")
setnames(event.etm,"ChargeType","type")
setnames(event.etm,"chargeDist","dist")
setnames(event.etm,"Time","time")
event.etm <- event.etm[,c("year","TAZ","dist","type","time")]
event.etm[,trip:='etm']

event <- rbind(event.sd,event.ld,event.etm)
setorder(event,year,type,TAZ)

event.home <- event[type=='H']
event.wp <- event[type=='W'|type=="P"]

# split the new events for each year
n.event.home <- event.home[,.(n_event=.N),by=.(TAZ,year)]
n.event.wp <- event.wp[,.(n_event=.N),by=.(TAZ,year)]

share.block.TAZ <- fread('data/mapping/census block to TAZ 2010/share_block vs TAZ.csv')
head(share.block.TAZ)
setnames(share.block.TAZ,"TAZ_Zone","TAZ")

# for now, get rid of the TAZs that have no population/jobs at all but still have home/workplace/public charging events
share.block.TAZ[,year:=2022]
share.block.TAZ.years <- data.table()
for (y in 2022:2045){
  share.block.TAZ[,year:=y]
  share.block.TAZ.years <- rbind(share.block.TAZ.years,share.block.TAZ)
}

n.event.home.block <- merge(share.block.TAZ.years,n.event.home,by=c("TAZ","year"))
setnames(n.event.home.block,"n_event","n_event_TAZ")
#conflict.TAZ.home <- n.event.home.block[population_TAZ==0] # 4 TAZs
n.event.home.block <- n.event.home.block[population_TAZ!=0]

n.event.wp.block <- merge(share.block.TAZ.years,n.event.wp,by=c("TAZ","year"))
setnames(n.event.wp.block,"n_event","n_event_TAZ")
#conflict.TAZ.wp <- n.event.wp.block[jobs_TAZ==0] # 5 TAZs
n.event.wp.block <- n.event.wp.block[jobs_TAZ!=0]

# ======= calculate number of events in each block: split integer into integers ====================
# 1.get floor of the divided numbers & calculate residual
# 2.generate random IDs of the events that are in TAZs where population/job is not 0
# 3.add the residual back to the first IDs of each group (TAZ,year)
set.seed(66)
n.event.home.block <- n.event.home.block[,c("year","TAZ","GEOID10","population_share","n_event_TAZ")]
n.event.home.block[,int:=floor(n_event_TAZ*population_share)]
n.event.home.block[,res:=n_event_TAZ-sum(int),by=.(TAZ,year)]
n.event.home.block[,n_event_block:=int]
#n.event.home.block[,ID:=.I]
#n.event.home.block[,ID_TAZ_y_nonz:=0]
#n.event.home.block[population_share>0,ID_TAZ_y_nonz:=rowid(TAZ,year)]
n.event.home.block[,ID_TAZ_y_nonz:=0]
n.event.home.block[population_share>0,ID_TAZ_y_nonz:=sample(.N,.N),by=.(TAZ,year)]
n.event.home.block[ID_TAZ_y_nonz>0 & ID_TAZ_y_nonz<=res,
                   n_event_block:=n_event_block+1]
set.seed(66)
n.event.wp.block <- n.event.wp.block[,c("year","TAZ","GEOID10","jobs_share","n_event_TAZ")]
n.event.wp.block[,int:=floor(n_event_TAZ*jobs_share)]
n.event.wp.block[,res:=n_event_TAZ-sum(int),by=.(TAZ,year)]
n.event.wp.block[,n_event_block:=int]
n.event.wp.block[,ID_TAZ_y_nonz:=0]
n.event.wp.block[jobs_share>0,ID_TAZ_y_nonz:=sample(.N,.N),by=.(TAZ,year)]
n.event.wp.block[ID_TAZ_y_nonz>0 & ID_TAZ_y_nonz<=res,
                 n_event_block:=n_event_block+1]

# n.event.wp.block[,res_check:=n_event_TAZ-sum(n_event_block),by=.(TAZ,year)]
# summary(n.event.wp.block$res_check)
# head(n.event.wp.block)

# ==================== split charging events from TAZ to blocks ================
# generate a random order ID of charging events in each TAZ
set.seed(99)
event.home[,order:=sample(.N,.N),by=.(TAZ,year)]
# the order ID range that each block corresponds to
n.event.home.block[,start:=cumsum(shift(n_event_block, n = 1, fill = 0)),by=.(TAZ,year)]
n.event.home.block[,end:=start+n_event_block]
# join the blocks to the events according to the order IDs
event.home.split <- event.home[n.event.home.block,
                               on=.(year,TAZ,order>start,order<=end),
                               .(year,TAZ,dist,type,time,trip,GEOID10,n_event_block,n_event_TAZ)][!is.na(dist)]
# 28500 events disappeared - those are in the TAZs that have no population at all

# generate a random order ID of charging events in each TAZ
set.seed(99)
event.wp[,order:=sample(.N,.N),by=.(TAZ,year)]
# the order ID range that each block corresponds to
n.event.wp.block[,start:=cumsum(shift(n_event_block, n = 1, fill = 0)),by=.(TAZ,year)]
n.event.wp.block[,end:=start+n_event_block]
# join the blocks to the events according to the order IDs
event.wp.split <- event.wp[n.event.wp.block,
                           on=.(year,TAZ,order>start,order<=end),
                           .(year,TAZ,dist,type,time,trip,GEOID10,n_event_block,n_event_TAZ)][!is.na(dist)]
# 32942 events disappeared - those are in the TAZs that have no population at all

head(event.wp.split)
head(event.home.split)

saveRDS(event.home.split,file='data/mobility data/CSTDM processed/charge_event_new_block_home_sample42_draw12_split6699.rds')
saveRDS(event.wp.split,file='data/mobility data/CSTDM processed/charge_event_new_block_workpublic_sample42_draw12_split6699.rds')
