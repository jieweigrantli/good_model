library(data.table)

event.feeder <- readRDS('data/result/charging session by feeder/session_feeder_new_bins_IOU_sample42_draw12_split6699_sample36.rds')
head(event.feeder)

length(unique(event.feeder[year==2022]$FeederID)) # 7926
length(unique(event.feeder[year==2023]$FeederID)) # 7874
length(unique(event.feeder$FeederID)) # 7958/8032

## add up new charging profile separately by year (by feeder by type)
feeder.prof.new.byyear <- data.table()
for (y in 2022:2045) {
  event.feeder.temp <- event.feeder[year==y]
  # 24h charging profile for each event
  feeder.prof.temp <- event.feeder.temp[rep(1:nrow(event.feeder.temp),each=24),]
  feeder.prof.temp[,hour:=rep(0:23,times=nrow(event.feeder.temp))]
  feeder.prof.temp[,load:=0]
  # same day
  feeder.prof.temp[hour>=start_hour & hour<=end_hour,load:=power] 
  # charge till tomorrow, add back to the beginning of today
  feeder.prof.temp[start_hour>end_hour & (hour>=start_hour|hour<=end_hour),load:=power] 
  feeder.prof.temp <- feeder.prof.temp[,.(load_feeder=sum(load)),by=.(FeederID,charge_type,hour)]
  feeder.prof.temp[,year:=y]
  
  feeder.prof.new.byyear <- rbind(feeder.prof.new.byyear,feeder.prof.temp)
}

head(feeder.prof.new.byyear)

## add up charging profile cumulatively by year (by feeder by type)
feeder.prof.cum.byyear <- data.table()
# for now, consider only the feeders that have EV charging load
# if needed, can be changed into all valid feeders from the feeder-block mapping table
all.feeder <- unique(event.feeder$FeederID)

for (y in 2022:2045){
  # make space for all feeders (EV load does not increase in every feeder each year)
  prof.temp.allfeeder <- data.table(FeederID=rep(all.feeder,each=3),
                                    charge_type=rep(c('home','work','public'),times=length(all.feeder)))
  prof.temp.allfeeder <- prof.temp.allfeeder[rep(1:nrow(prof.temp.allfeeder),each=24),]
  prof.temp.allfeeder[,hour:=rep(0:23,times=length(all.feeder)*3)]
  # add up the existing profile for all years so far
  feeder.prof.new.temp <- feeder.prof.new.byyear[year<=y]
  feeder.prof.cum.temp <- feeder.prof.new.temp[,.(load=sum(load_feeder)),by=.(FeederID,charge_type,hour)]
  # merge it into all feeders
  prof.temp.allfeeder <- feeder.prof.cum.temp[prof.temp.allfeeder,on=.(FeederID,charge_type,hour)]
  # the feeders where EV load haven't appeared yet should be set to 0
  prof.temp.allfeeder[is.na(load),load:=0]
  
  prof.temp.allfeeder[,year:=y]
  feeder.prof.cum.byyear <- rbind(feeder.prof.cum.byyear,prof.temp.allfeeder)
}

head(feeder.prof.cum.byyear)

saveRDS(feeder.prof.cum.byyear,'data/result/charging profile by feeder/profile_feeder_cum_bins_IOU_sample42_draw12_split6699_sample36.rds')
