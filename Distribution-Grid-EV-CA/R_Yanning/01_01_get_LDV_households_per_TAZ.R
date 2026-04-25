library(data.table)

# ============== Short Distance Private Trips =================================
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

TAZ.hh.sd.all <- all.trips.sd[Mode=='SOV'|Mode=='HOV2'|Mode=='HOV3',
                               c('SerialNo','HomeZone')]
TAZ.hh.sd <- unique(TAZ.hh.sd.all)
setwd('C:/Users/Yanning Li/Box Sync/Distribution Grid/Distribution Grid EV CA')
fwrite(TAZ.hh.sd,file='data/mobility data/CSTDM processed/SDPTM_hh_home_TAZ.csv',row.names=FALSE)

# ============== Long Distance Private Trips =================================
all.trips.ld <- fread('C:/Nina/Research (large files)/CSTDM/LDPTM/LDPTM_Trips.csv')

TAZ.hh.ld.all <- all.trips.ld[Mode=='SOV'|Mode=='HOV2'|Mode=='HOV3',
                              c('SerialNo','HomeZone')]
TAZ.hh.ld <- unique(TAZ.hh.ld.all)
fwrite(TAZ.hh.ld,file='data/mobility data/CSTDM processed/LDPTM_hh_home_TAZ.csv',row.names=FALSE)

all.trips.ld.accegr <- fread('C:/Nina/Research (large files)/CSTDM/LDPTM/LDPTM_AccEgr.csv')
nrow(all.trips.ld.accegr[is.na(AccMode)])
nrow(all.trips.ld.accegr[is.na(EgrMode)])

TAZ.hh.ld.accegr.all <- all.trips.ld.accegr[(DPurp=='Access'&AccMode=='Drive_Park')|(OPurp=='Egress'&EgrMode=='Rent_Car'),
                                            c('SerialNo','HomeZone')]
TAZ.hh.ld.accegr <- unique(TAZ.hh.ld.accegr.all)
fwrite(TAZ.hh.ld.accegr,file='data/mobility data/CSTDM processed/LDPTM_AccEgr_hh_home_TAZ.csv',row.names=FALSE)

# ============== ETM =================================
all.trips.etm <- fread('C:/Nina/Research (large files)/CSTDM/ETM/trips_Ext.csv')

# only the LDV trips that enter CA
TAZ.hh.etm.all <- all.trips.etm[(ActorType=='CarLong'|ActorType=='CarLocal')&(Mode=='SOV'|Mode=='HOV2'|Mode=='HOV3') & DPurp=='I',
                                c('SerialNo','HomeZone')]

TAZ.hh.etm <- unique(TAZ.hh.etm.all)
fwrite(TAZ.hh.etm,file='data/mobility data/CSTDM processed/ETM_hh_home_TAZ.csv',row.names=FALSE)

# ============= start from here if all trips already processed ======================
setwd('C:/Users/Yanning Li/Box Sync/Distribution Grid/Distribution Grid EV CA')
TAZ.hh.sd <- fread('data/mobility data/CSTDM processed/SDPTM_hh_home_TAZ.csv')
TAZ.hh.ld <- fread('data/mobility data/CSTDM processed/LDPTM_hh_home_TAZ.csv')
TAZ.hh.ld.accegr <- fread('data/mobility data/CSTDM processed/LDPTM_AccEgr_hh_home_TAZ.csv')
TAZ.hh.etm <- fread('data/mobility data/CSTDM processed/ETM_hh_home_TAZ.csv')

# # Put the household pool inside CA together (ETM separately)
# TAZ.hh.CA.all <- rbind(TAZ.hh.sd,TAZ.hh.ld,TAZ.hh.ld.accegr)
# TAZ.hh.CA <- unique(TAZ.hh.CA.all)
# 
# fwrite(TAZ.hh.CA,file='data/mobility data/CSTDM processed/CA_hh_home_TAZ.csv',row.names=FALSE)
