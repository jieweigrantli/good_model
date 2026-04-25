library(data.table)

TAZ.hh.sd <- fread('data/mobility data/CSTDM processed/SDPTM_hh_home_TAZ.csv')
TAZ.hh.ld <- fread('data/mobility data/CSTDM processed/LDPTM_hh_home_TAZ.csv')
TAZ.hh.ld.accegr <- fread('data/mobility data/CSTDM processed/LDPTM_AccEgr_hh_home_TAZ.csv')
TAZ.hh.etm <- fread('data/mobility data/CSTDM processed/ETM_hh_home_TAZ.csv')

TAZ.hh.share <- fread('data/mobility data/EV Toolbox/evhh_share_TAZ.csv')

CA.hh.share <- TAZ.hh.share[,.(EVhh_CA=sum(EVhh_TAZ),hh_CA=sum(hh_TAZ)),by=.(year)]
CA.hh.share[,EVhh_share:=EVhh_CA/hh_CA]

# length(intersect(unique(TAZ.hh.share$TAZ),unique(TAZ.hh.sd$HomeZone)))
# length(unique(TAZ.hh.sd$HomeZone))
# length(unique(TAZ.hh.share$TAZ))

########### EV hh count in each TAZ #######################################
# short distance
TAZ.hh.sd.count <- TAZ.hh.sd[,.(n_hh=.N),by=.(HomeZone)]
TAZ.EVhh.sd.count <- data.table()
for (y in 2022:2045){
  TAZ.EVhh.sd.count <- rbind(TAZ.EVhh.sd.count,TAZ.hh.sd.count[,year:=y])
}
TAZ.EVhh.sd.count <- merge(x=TAZ.EVhh.sd.count,y=TAZ.hh.share[,c("year","TAZ","EVhh_share")],
                           by.x=c("year","HomeZone"),by.y=c("year","TAZ"))
TAZ.EVhh.sd.count[,n_EVhh:=n_hh*EVhh_share] 
TAZ.EVhh.sd.count[,n_EVhh_newf:=n_EVhh-shift(n_EVhh),by=HomeZone] 
TAZ.EVhh.sd.count[year==2022,n_EVhh_newf:=n_EVhh]
TAZ.EVhh.sd.count[,n_EVhh_new:=floor(n_EVhh_newf)]
# long distance
TAZ.hh.ld.count <- TAZ.hh.ld[,.(n_hh=.N),by=.(HomeZone)]
TAZ.EVhh.ld.count <- data.table()
for (y in 2022:2045){
  TAZ.EVhh.ld.count <- rbind(TAZ.EVhh.ld.count,TAZ.hh.ld.count[,year:=y])
}
TAZ.EVhh.ld.count <- merge(x=TAZ.EVhh.ld.count,y=TAZ.hh.share[,c("year","TAZ","EVhh_share")],
                           by.x=c("year","HomeZone"),by.y=c("year","TAZ"))
TAZ.EVhh.ld.count[,n_EVhh:=n_hh*EVhh_share] 
TAZ.EVhh.ld.count[,n_EVhh_newf:=n_EVhh-shift(n_EVhh),by=HomeZone] 
TAZ.EVhh.ld.count[year==2022,n_EVhh_newf:=n_EVhh]
TAZ.EVhh.ld.count[,n_EVhh_new:=floor(n_EVhh_newf)]
# long distance acc/egr
TAZ.hh.ld.accegr.count <- TAZ.hh.ld.accegr[,.(n_hh=.N),by=.(HomeZone)]
TAZ.EVhh.ld.accegr.count <- data.table()
for (y in 2022:2045){
  TAZ.EVhh.ld.accegr.count <- rbind(TAZ.EVhh.ld.accegr.count,TAZ.hh.ld.accegr.count[,year:=y])
}
TAZ.EVhh.ld.accegr.count <- merge(x=TAZ.EVhh.ld.accegr.count,y=TAZ.hh.share[,c("year","TAZ","EVhh_share")],
                           by.x=c("year","HomeZone"),by.y=c("year","TAZ"))
TAZ.EVhh.ld.accegr.count[,n_EVhh:=n_hh*EVhh_share] 
TAZ.EVhh.ld.accegr.count[,n_EVhh_newf:=n_EVhh-shift(n_EVhh),by=HomeZone] 
TAZ.EVhh.ld.accegr.count[year==2022,n_EVhh_newf:=n_EVhh]
TAZ.EVhh.ld.accegr.count[,n_EVhh_new:=floor(n_EVhh_newf)]

# ext - keep the zones (freeways), use aggregate EV hh share in CA (aggressive)
#TAZ.hh.etm.count <- TAZ.hh.etm[,.(n_hh=.N)]
TAZ.hh.etm.count <- TAZ.hh.etm[,.(n_hh=.N),by=.(HomeZone)]
TAZ.EVhh.etm.count <- data.table()
for (y in 2022:2045){
  TAZ.EVhh.etm.count <- rbind(TAZ.EVhh.etm.count,TAZ.hh.etm.count[,year:=y])
}
TAZ.EVhh.etm.count <- merge(x=TAZ.EVhh.etm.count,y=CA.hh.share[,c("year","EVhh_share")],
                           by="year")
TAZ.EVhh.etm.count[,n_EVhh:=n_hh*EVhh_share] 
TAZ.EVhh.etm.count[,n_EVhh_newf:=n_EVhh-shift(n_EVhh),by=HomeZone] 
TAZ.EVhh.etm.count[year==2022,n_EVhh_newf:=n_EVhh]
TAZ.EVhh.etm.count[,n_EVhh_new:=floor(n_EVhh_newf)]

######### sample EV hh in each TAZ each year ####################################
# NOTE: the hhs are NEW EVhh in each year (need to accumulate when used later)

sample.EVhh <- function(TAZ.hh,TAZ.EVhh.count){
  TAZ.EVhh <- data.table()
  TAZ.hh.temp <- TAZ.hh[HomeZone%in%TAZ.EVhh.count$HomeZone]
  set.seed(42)
  for (y in 2022:2045){
    TAZ.EVhh.pool <- merge(x=TAZ.hh.temp,y=TAZ.EVhh.count[year==y,c("year","HomeZone","n_EVhh_new")],
                              by="HomeZone")
    
    TAZ.EVhh.temp <- TAZ.EVhh.pool[,sample(x=SerialNo,size=min(n_EVhh_new)),by=HomeZone]
    TAZ.EVhh.temp[,year:=y]
    setnames(TAZ.EVhh.temp,"V1","hhID")
    
    TAZ.EVhh <- rbind(TAZ.EVhh,TAZ.EVhh.temp)
    
    TAZ.hh.temp <- TAZ.hh.temp[!(SerialNo%in%TAZ.EVhh$hhID)] 
  }
  return(TAZ.EVhh)
}

# long distance
TAZ.EVhh.ld <- sample.EVhh(TAZ.hh.ld,TAZ.EVhh.ld.count)
fwrite(TAZ.EVhh.ld,file='data/mobility data/CSTDM processed/EVhh_new_LDPTM_sample42.csv',row.names=FALSE)

# short distance
TAZ.EVhh.sd <- sample.EVhh(TAZ.hh.sd,TAZ.EVhh.sd.count)
fwrite(TAZ.EVhh.sd,file='data/mobility data/CSTDM processed/EVhh_new_SDPTM_sample42.csv',row.names=FALSE)

# long distance access/egress
TAZ.EVhh.ld.accegr <- sample.EVhh(TAZ.hh.ld.accegr,TAZ.EVhh.ld.accegr.count)
fwrite(TAZ.EVhh.ld.accegr,file='data/mobility data/CSTDM processed/EVhh_new_LDPTM_AccEgr_sample42.csv',row.names=FALSE)

# etm 
TAZ.EVhh.etm <- sample.EVhh(TAZ.hh.etm,TAZ.EVhh.etm.count)
fwrite(TAZ.EVhh.etm,file='data/mobility data/CSTDM processed/EVhh_new_ETM_sample42.csv',row.names=FALSE)



