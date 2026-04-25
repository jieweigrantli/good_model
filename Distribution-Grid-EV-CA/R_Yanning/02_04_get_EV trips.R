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

TAZ.dist.LD <- load.files(directory='data/mobility data/TAZ distance/parsed_100000_LDPTM')
TAZ.dist.ETM <- load.files(directory='data/mobility data/TAZ distance/parsed_100000_ETM')

TAZ.dist.LD.short <- TAZ.dist.LD[,c("from","to","distance")]
TAZ.dist.LD.short <- unique(TAZ.dist.LD.short)
TAZ.dist.ETM.short <- TAZ.dist.ETM[,c("from","to","distance")]
TAZ.dist.ETM.short <- unique(TAZ.dist.ETM.short)

## don't touch! ####
load.trips <- function() {
  files.all <- list.files()
  output <- data.table()
  for(f in files.all) {
    hold <- fread(f)
    output <- rbind(output,hold)
  }
  return(output)
}

setwd('C:/Nina/Research (large files)/CSTDM/SDPTM')
all.trips.sd <- load.trips()
all.trips.sd.short <- all.trips.sd[,c("SerialNo","Person","Tour","Trip","DPurp","I","J","Mode","Dist","Time")]
setwd('C:/Users/nina3/Box Sync/Distribution Grid/Distribution Grid EV CA')
saveRDS(all.trips.sd.short,file='data/mobility data/CSTDM processed/SDPTM trips short.rds')

#### start from here if already saved the short version
setwd('C:/Users/nina3/Box Sync/Distribution Grid/Distribution Grid EV CA')


# short distance EV trips
EVhh.sd <- fread('data/mobility data/CSTDM processed/EVhh_new_SDPTM_sample42.csv')
all.trips.sd.short <- readRDS('data/mobility data/CSTDM processed/SDPTM trips short.rds')
EV.trips.sd <- all.trips.sd.short[SerialNo%in%EVhh.sd$hhID & (Mode=='SOV'|Mode=='HOV2'|Mode=='HOV3'),
                            c("SerialNo","Person","Tour","Trip","DPurp","I","J","Mode","Dist","Time")]
EV.trips.sd <- merge(x=EV.trips.sd,y=EVhh.sd[,c("hhID","year")],
                     by.x="SerialNo",by.y="hhID")
#EV.trips.sd <- readRDS('data/mobility data/CSTDM processed/EV trips_new_SDPTM_sample42.rds')

aggregate.purpose <- data.table('ChargeType'=c('H','W','W','P','P','P','P','P','P','P','P'),
                                    'DPurp'=c('O','W','P','S','H','T','C','L','R','K','Z'))
EV.trips.sd <- merge(x=EV.trips.sd,y=aggregate.purpose,by='DPurp')

saveRDS(EV.trips.sd,file='data/mobility data/CSTDM processed/EV trips_new_SDPTM_sample42.rds')

# long distance EV trips
EVhh.ld <- fread('data/mobility data/CSTDM processed/EVhh_new_LDPTM_sample42.csv')
all.trips.ld <- fread('C:/Nina/Research (large files)/CSTDM/LDPTM/LDPTM_Trips.csv')
EV.trips.ld <- all.trips.ld[SerialNo%in%EVhh.ld$hhID & (Mode=='SOV'|Mode=='HOV2'|Mode=='HOV3'),
                            c("SerialNo","Person","Tour","Trip","DPurp","I","J","Mode","Dist","Time")]
EV.trips.ld <- merge(x=EV.trips.ld,y=EVhh.ld[,c("hhID","year")],
                     by.x="SerialNo",by.y="hhID")

aggregate.purpose <- data.table('ChargeType'=c('W','H','P','W','P'),
                                'DPurp'=c('Bus','VFR','Rec','Com','Oth'))
EV.trips.ld <- merge(x=EV.trips.ld,y=aggregate.purpose,by='DPurp')

saveRDS(EV.trips.ld,file='data/mobility data/CSTDM processed/EV trips_new_LDPTM_sample42.rds')

### just to check if the distances from the matrix all match
EV.trips.ld <- readRDS(file='data/mobility data/CSTDM processed/EV trips_new_LDPTM_sample42.rds')
EV.trips.ld.dist <- merge(x=EV.trips.ld,y=TAZ.dist.LD.short,
                          by.x=c("I","J"),by.y=c("from","to"),
                          all.x=T,allow.cartesian=T)
summary(EV.trips.ld.dist$distance)

trips.ld.dist <- merge(x=all.trips.ld,y=TAZ.dist.LD.short,
                          by.x=c("I","J"),by.y=c("from","to"),
                          all.x=T,allow.cartesian=T)
summary(trips.ld.dist$distance)

trips.ld.dist[,distance_mile:=distance*0.000621371192]
head(trips.ld.dist)
trips.ld.dist[,delta_dist:=Dist-distance_mile]
summary(trips.ld.dist$delta_dist) # difference range from -153.65 to 34.57 miles, mean -21.43

# long distance AccEgr EV trips - No Dist
EVhh.ld.accegr <- fread('data/mobility data/CSTDM processed/EVhh_new_LDPTM_AccEgr_sample42.csv')
all.trips.ld.accegr <- fread('C:/Nina/Research (large files)/CSTDM/LDPTM/LDPTM_AccEgr.csv')
unique(all.trips.ld.accegr$Mode)
unique(all.trips.ld.accegr$AccMode)
unique(all.trips.ld.accegr$DPurp)

EV.trips.ld.accegr <- all.trips.ld.accegr[SerialNo%in%EVhh.ld.accegr$hhID 
                                          & ((DPurp=='Access'&AccMode=='Drive_Park')|(OPurp=='Egress'&EgrMode=='Rent_Car')),
                                          c("SerialNo","DPurp","I","J","Mode","AccMode","EgrMode","Time")]
EV.trips.ld.accegr <- merge(x=EV.trips.ld.accegr,y=EVhh.ld.accegr[,c("hhID","year")],
                            by.x="SerialNo",by.y="hhID")

unique(EV.trips.ld.accegr$DPurp)
aggregate.purpose <- data.table('ChargeType'=c('W','H','P','W','P','P'),
                                'DPurp'=c('Bus','VFR','Rec','Com','Oth','Access'))
EV.trips.ld.accegr <- merge(x=EV.trips.ld.accegr,y=aggregate.purpose,by='DPurp')

saveRDS(EV.trips.ld.accegr,file='data/mobility data/CSTDM processed/EV trips_new_LDPTM_AccEgr_sample42.rds')

EV.trips.ld.accegr.dist <- merge(x=EV.trips.ld.accegr,y=TAZ.dist.short,
                            by.x=c("I","J"),by.y=c("from","to"))
# only 5 trips can find a match in the distance matrix, might need to discard this category of trips


# EXT EV trips - No Dist
all.trips.ext <- fread('C:/Nina/Research (large files)/CSTDM/ETM/trips_Ext.csv')
EVhh.ext <- fread('data/mobility data/CSTDM processed/EVhh_new_ETM_sample42.csv')

EV.trips.ext <- all.trips.ext[SerialNo%in%EVhh.ext$hhID 
                              & (ActorType=='CarLong'|ActorType=='CarLocal')
                              & (Mode=='SOV'|Mode=='HOV2'|Mode=='HOV3') 
                              & DPurp=='I',
                              c("SerialNo","Person","Tour","Trip","DPurp","I","J","Mode","ActorType","Time")]
EV.trips.ext <- merge(x=EV.trips.ext,y=EVhh.ext[,c("hhID","year")],
                      by.x="SerialNo",by.y="hhID")
EV.trips.ext[,ChargeType:="P"]
EV.trips.ext.dist <- merge(x=EV.trips.ext,y=TAZ.dist.ETM.short,
                           by.x=c("I","J"),by.y=c("from","to"))
EV.trips.ext.dist[,Dist:=distance/1609.344]
summary(EV.trips.ext.dist)

saveRDS(EV.trips.ext.dist,file='data/mobility data/CSTDM processed/EV trips_new_EXT_sample42.rds')


# y <- 2044
# EV.trips <- all.trips.sd[SerialNo%in%EVhh.sd[year<=y]$hhID & (Mode=='SOV'|Mode=='HOV2'|Mode=='HOV3'),
#                          c("SerialNo","Person","Tour","Trip","DPurp","J","Mode","Dist")]
# saveRDS(EV.trips,file=paste0('data/mobility data/CSTDM processed/EV trips_SDPTM_',y,'_sample42'))
# EV.trips <- readRDS(paste0('data/mobility data/CSTDM processed/EV trips_SDPTM_',y,'_sample42'))

