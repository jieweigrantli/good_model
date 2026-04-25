library(data.table)

home.event.block <- readRDS('data/mobility data/CSTDM processed/charge_event_new_block_home_sample42_draw12_split6699.rds')
wp.event.block <- readRDS('data/mobility data/CSTDM processed/charge_event_new_block_workpublic_sample42_draw12_split6699.rds')

block.to.feeder <- fread('data/mapping/block to feeder 2010/shape mapped/block_2010_to_feeder_map_line_combo.csv')

event.pool <- readRDS("data/charging data/charging_session_all_clean.rds")

IOU.blocks <- fread('data/mapping/block to feeder 2010/shape block IOUs++/shape block IOUs++.csv')

head(home.event.block)
head(wp.event.block)
head(block.to.feeder)

event.block.all <- rbind(home.event.block,wp.event.block)
event.block.all <- event.block.all[GEOID10%in%IOU.blocks$GEOID10]

event.block.all <- merge(event.block.all,block.to.feeder,by="GEOID10")

# add bins
event.block.all[,demand:=dist/3]
event.block.all[demand<=5,bin:=1]
event.block.all[demand>5 & demand<=10,bin:=2]
event.block.all[demand>10 & demand<=15,bin:=3]
event.block.all[demand>15 & demand<=20,bin:=4]
event.block.all[demand>20 & demand<=30,bin:=5]
event.block.all[demand>30 & demand<=50,bin:=6]
event.block.all[demand>50 & demand<=80,bin:=7]
event.block.all[demand>80,bin:=8]

event.pool[energy<=5,bin:=1]
event.pool[energy>5 & energy<=10,bin:=2]
event.pool[energy>10 & energy<=15,bin:=3]
event.pool[energy>15 & energy<=20,bin:=4]
event.pool[energy>20 & energy<=30,bin:=5]
event.pool[energy>30 & energy<=50,bin:=6]
event.pool[energy>50 & energy<=80,bin:=7]
event.pool[energy>80,bin:=8]

head(event.block.all)
head(event.pool)

############## sample with weight:level in public & housing in home ##############

### public: DC/L2 #######
level.weight <- data.table(level=c('DC','L2'),
                           ncharger=c(8919,29229))
n.event <- nrow(event.block.all[type=='P'])
set.seed(36)
event.block.all[,level:='none']
event.block.all[type=='P',
                level:=sample(level.weight$level,
                              n.event,
                              prob=level.weight$ncharger,
                              replace=T)]

nrow(event.block.all[level=='DC']) #3750433
nrow(event.block.all[level=='L2']) #12289247
# ratio very close to charger ratio

event.block.all[trip=='ld_corridor',level:='DC']
nrow(event.block.all[level=='DC']) #4125536
nrow(event.block.all[level=='L2']) #11914144
# ratio from 3.3 -> 2.9 (more DC added to corridor)

### home: single/multi #######
unique(event.pool$housing)
housing.weight <- data.table(house=c('single family','multi family'),
                             unit=c(56.4,38.9))
n.event <- nrow(event.block.all[type=='H'])

set.seed(36)
event.block.all[,house:='none']
event.block.all[type=='H',
                house:=sample(housing.weight$house,
                              n.event,
                              prob=housing.weight$unit,
                              replace=T)]

nrow(event.block.all[house=='single family']) #28712855
nrow(event.block.all[house=='multi family']) #19809304
# ratio very close to charger ratio

############## sample #########################################################
event.block.all[,n_event:=.N,keyby=.(type,level,house,bin)]
head(event.block.all)

event.pool[,eventID:=.I]
head(event.pool)

### home ###
start_time <- Sys.time()
for (h in c('single family','multi family')){
  for(b in 1:8){
    set.seed(36)
    event.block.all[type=='H' & house==h & bin==b,
                    eventID:=sample(event.pool[charge_type=='home' 
                                               & housing==h 
                                               & bin==b]$eventID,
                                    min(n_event),
                                    replace=T)]
  }
}
end_time <- Sys.time()
end_time - start_time # 4 secs

# summary(event.block.all$eventID)
# summary(event.block.all[type=='H']$eventID)

### public ###
start_time <- Sys.time()
for (l in c('DC','L2')){
  for(b in 1:8){
    set.seed(36)
    event.block.all[type=='P' & level==l & bin==b,
                    eventID:=sample(event.pool[charge_type=='public' 
                                               & charger_level==l 
                                               & bin==b]$eventID,
                                    min(n_event),
                                    replace=T)]
  }
}
end_time <- Sys.time()
end_time - start_time # 3 secs

### work ###
start_time <- Sys.time()
for(b in 1:8){
  set.seed(36)
  event.block.all[type=='W' & bin==b,
                  eventID:=sample(event.pool[charge_type=='work' 
                                             & bin==b]$eventID,
                                  min(n_event),
                                  replace=T)]
}
end_time <- Sys.time()
end_time - start_time # 2 secs

#summary(event.block.all$eventID) # all sampled

############## join empirical event into sampled IDs ####################################
event.feeder.sample <- event.pool[event.block.all,on=.(eventID)]
head(event.feeder.sample)

event.feeder.sample <- event.feeder.sample[,c('FeederID','year',
                                              'start_hour','end_hour',
                                              'charge_type','charger_level',
                                              'power','energy','housing')]

saveRDS(event.feeder.sample,'data/result/charging session by feeder/session_feeder_new_bins_IOU_sample42_draw12_split6699_sample36.rds')

