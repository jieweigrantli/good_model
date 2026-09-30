library(data.table)

load.files <- function(directory) {
  files.all <- list.files(path=directory)
  output <- data.table()
  for(f in files.all) {
    hold <- fread(paste0(directory,'/',f))
    output <- rbind(output,hold)
  }
  return(output)
}
########### LDPTM ################################################################
dist.ld <- load.files(directory='data/mobility data/TAZ distance/parsed_100000_LDPTM')
EV.trips.ld <- readRDS('data/mobility data/CSTDM processed/EV trips_new_LDPTM_sample42.rds')
summary(EV.trips.ld$Dist) # 100-742, mean 187

setorder(EV.trips.ld,year,SerialNo,Person,Tour,Trip)
EV.trips.ld[, TripID := 1:nrow(EV.trips.ld)]
EV.trips.ld <- EV.trips.ld[,c("TripID",'year',"I","J",'Dist','Time','ChargeType')]

setnames(dist.ld,"from","I")
setnames(dist.ld,"to","J")
setnames(dist.ld,"TAZ12,","TAZway")

# option 1: split all the trips evenly in the TAZs on the way
trips.route.ld <- dist.ld[,c("TAZway","I","J")][EV.trips.ld,on=.(I,J)]
# assume charging in the origin TAZ? if not, need to remove the origin TAZ here
trips.route.ld[, nTAZ:=.N,by=TripID]
trips.route.ld[,chargeDist:=Dist/nTAZ]
summary(trips.route.ld$chargeDist) # 0.8-33, mean 2.1
# all public charging except at destination TAZ
trips.route.ld[,ChargeEventType:='P']
trips.route.ld[TAZway==J,ChargeEventType:=ChargeType]
head(trips.route.ld)

# option 2: split only the trips >300 miles
events.ld.short <- EV.trips.ld[Dist<=300] # 1772249/197917
head(events.ld.short) 
events.ld.short[,TAZway:=J] # charge at the destination TAZ
events.ld.short[,nTAZ:=1]
events.ld.short[,chargeDist:=Dist]
events.ld.short[,ChargeEventType:=ChargeType]
summary(events.ld.short$chargeDist) # 100-300, mean 159

events.ld.long <- EV.trips.ld[Dist>300] # 25668/197917
events.ld.long <- dist.ld[,c("TAZway","I","J")][events.ld.long,on=.(I,J)]
events.ld.long <- events.ld.long[TAZway!=I] # assume no charging in the origin TAZ
events.ld.long[, nTAZ:=.N,by=TripID]
events.ld.long[,chargeDist:=Dist/nTAZ]
summary(events.ld.long$chargeDist) # 1.5-23, mean 2.6
# all public charging except at destination TAZ
events.ld.long[,ChargeEventType:='P']
events.ld.long[TAZway==J,ChargeEventType:=ChargeType]

head(events.ld.long)
head(events.ld.short)

events.ld <- rbind(events.ld.long,events.ld.short)

# option 3: charge at the TAZs that overlap with the most trips (0.1 of all TAZs)
dist.ld[,TAZfreq:=.N,by=TAZway]
TAZ.freq <- unique(dist.ld[,c("TAZway","TAZfreq")])
summary(TAZ.freq) # min 2

trips.route.ld <- dist.ld[,c("TAZway","I","J")][EV.trips.ld,on=.(I,J)]
trips.route.ld <- TAZ.freq[trips.route.ld,on=.(TAZway)]

trips.route.ld[,freqCUT:=quantile(x=TAZfreq,probs=0.9),by=TripID]
events.ld <- trips.route.ld[TAZfreq>freqCUT]
events.ld[, nTAZ:=.N,by=TripID]
events.ld[,chargeDist:=Dist/nTAZ]
summary(events.ld$chargeDist) # 8-216, mean 20

# option 4: charge at the TAZs that overlap with the most trips (enable charging every 100 miles)
dist.ld[,TAZfreq:=.N,by=TAZway]
TAZ.freq <- unique(dist.ld[,c("TAZway","TAZfreq")])
summary(TAZ.freq) # min 2

trips.route.ld <- dist.ld[,c("TAZway","I","J")][EV.trips.ld,on=.(I,J)]
trips.route.ld <- TAZ.freq[trips.route.ld,on=.(TAZway)]

trips.route.ld[,nCharge:=ceiling(Dist/100)]
summary(trips.route.ld$nCharge) # 1-8
trips.route.ld[,nTAZtrip:=.N, by=TripID]
trips.route.ld[,qtCUT:=1-nCharge/nTAZtrip]
trips.route.ld[,freqCUT:=quantile(x=TAZfreq,probs=qtCUT),by=TripID]

events.ld <- trips.route.ld[TAZfreq>freqCUT]
events.ld[,nTAZcharge:=.N,by=TripID]
events.ld[,nDelta:=nTAZcharge-nCharge] # only for checking
summary(events.ld$nDelta) # min -1
summary(events.ld$nTAZcharge) # 1-8

events.ld[,chargeDist:=Dist/nTAZcharge]
summary(events.ld$chargeDist) # 50-147, mean 75
# all public charging except at destination TAZ
events.ld[,ChargeEventType:='P']
events.ld[TAZway==J,ChargeEventType:=ChargeType]
nrow(events.ld[ChargeEventType!='P']) # 1938/492853
# record corridor vs destination for further categorizing DC
events.ld[,other:='corridor']
events.ld[TAZway==J,other:='destination']
head(events.ld)

events.ld <- events.ld[,c('TripID','year','TAZway','I','J','Time','chargeDist','ChargeEventType','other')]
saveRDS(events.ld,
        file='data/mobility data/CSTDM processed/charge_event_new_LDPTM_sample42.rds')

########### ETM ################################################################
dist.etm <- load.files(directory='data/mobility data/TAZ distance/parsed_100000_ETM') # the route only includes the TAZs in CA
EV.trips.etm <- readRDS('data/mobility data/CSTDM processed/EV trips_new_EXT_sample42.rds')
summary(EV.trips.etm$Dist) # 1-513, mean 75

setorder(EV.trips.etm,year,SerialNo,Person,Tour,Trip)
EV.trips.etm[, TripID := 1:nrow(EV.trips.etm)]
EV.trips.etm <- EV.trips.etm[,c("TripID",'year',"I","J",'Dist','Time','ChargeType')]

setnames(dist.etm,"from","I")
setnames(dist.etm,"to",
         "J")
setnames(dist.etm,"TAZ12,","TAZway")

# option 1: split all the trips evenly in the TAZs on the way
trips.route.etm <- dist.etm[,c("TAZway","I","J")][EV.trips.etm,on=.(I,J),allow.cartesian=TRUE]
trips.route.etm[, nTAZ:=.N,by=TripID]
trips.route.etm[,chargeDist:=Dist/nTAZ]
summary(trips.route.etm$chargeDist) # 0.4-85, mean 3.7
saveRDS(trips.route.etm,file='data/mobility data/CSTDM processed/charge_event_new_ETM_sample42.rds')

# option 2: split only the trips >300 miles
events.etm.short <- EV.trips.etm[Dist<=300]
events.etm.short[,TAZway:=J] # charge at the destination TAZ
events.etm.short[,nTAZ:=1]
events.etm.short[,chargeDist:=Dist]
summary(events.etm.short$chargeDist) # 1.3-300, mean 75

events.etm.long <- EV.trips.etm[Dist>300]
events.etm.long <- dist.etm[,c("TAZway","I","J")][events.etm.long,on=.(I,J),allow.cartesian=TRUE]
events.etm.long[, nTAZ:=.N,by=TripID]
events.etm.long[,chargeDist:=Dist/nTAZ]
summary(events.etm.long$chargeDist) # 2-12, mean 5

events.etm <- rbind(events.etm.long,events.etm.short)

# option 4: charge at the TAZs that overlap with the most trips (enable charging every 100 miles)
dist.etm[,TAZfreq:=.N,by=TAZway]
TAZ.freq <- unique(dist.etm[,c("TAZway","TAZfreq")])
summary(TAZ.freq) # min 1

trips.route.etm <- dist.etm[,c("TAZway","I","J")][EV.trips.etm,on=.(I,J),allow.cartesian=TRUE]
trips.route.etm <- TAZ.freq[trips.route.etm,on=.(TAZway)]

trips.route.etm[,nCharge:=ceiling(Dist/100)]
summary(trips.route.etm$nCharge) # 1-6
trips.route.etm[,nTAZtrip:=.N, by=TripID]
trips.route.etm[,qtCUT:=1-nCharge/nTAZtrip]
trips.route.etm[,freqCUT:=quantile(x=TAZfreq,probs=qtCUT),by=TripID]
length(unique(trips.route.etm$TripID))

events.etm <- trips.route.etm[TAZfreq>=freqCUT]
length(unique(events.etm$TripID))
events.etm[,nTAZcharge:=.N,by=TripID]
events.etm[,nDelta:=nTAZcharge-nCharge] # only for checking
summary(events.etm$nDelta) # all 0

events.etm[,chargeDist:=Dist/nTAZcharge]
summary(events.etm$chargeDist) # 1.3-100, mean 53
summary(events.etm$Dist)

events.etm <- events.etm[,c('TripID','year','TAZway','I','J','Time','chargeDist','ChargeType')]
saveRDS(events.etm,
        file='data/mobility data/CSTDM processed/charge_event_new_ETM_sample42.rds')

######### check ###############
head(dist.etm)
head(EV.trips.etm)
head(trips.route.etm)
head(events.etm.long)
nrow(unique(EV.trips.ld[,c('I','J')]))
nrow(unique(dist.ld[,c('I','J')]))

all.trips.ld <- fread('C:/Nina/Research (large files)/CSTDM/LDPTM/LDPTM_Trips.csv')
nrow(unique(all.trips.ld[,c('I','J')]))
# only less than half of the trips are EV trips

dist.ld[,nTAZ:=.N,by=.(I,J)]

charge.event.sd <- readRDS(file='data/mobility data/CSTDM processed/charge_event_new_SDPTM_sample42_draw12.rds')
head(charge.event.sd)







