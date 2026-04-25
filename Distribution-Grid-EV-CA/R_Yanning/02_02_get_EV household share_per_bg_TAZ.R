library(tidyverse)
library(data.table)

# get household input and output from EV toolbox
household.all.tract <- fread('data/mobility data/EV Toolbox/households_all_by_tract.csv')
household.ev.tract <- fread('data/mobility data/EV Toolbox/evhouseholds_by_tract_acc2_2011.csv')

# tract population & block group population
pop.bg.tract <- fread('data/mapping/census bg to tract 2010/popluation share bg to tract 2010.csv')

# create table for yearly distribution of tracts to bgs
share.bg.tract <- pop.bg.tract[,year:=2022] %>%
  select(GEOIDtract,GEOIDbg,share,year)
share.bg.tract.years <- data.table()
for (y in 2022:2045){
  share.bg.tract[,year:=y]
  share.bg.tract.years <- rbind(share.bg.tract.years,share.bg.tract)
}

# distribute EV households from tracts to bgs
hh.ev.bg.tract <- merge(x=share.bg.tract.years,y=household.ev.tract,
                     by.x=c("GEOIDtract","year"),by.y=c("tract","year"))

setnames(hh.ev.bg.tract,"households","EVhh_tract")
hh.ev.bg.tract[,EVhh_bg:=EVhh_tract*share]
fwrite(hh.ev.bg.tract,file='data/mobility data/EV Toolbox/evhh_tract_bg_ACC2.csv',row.names=FALSE)

# distribute all households from tracts to bgs
hh.all.tract <- household.all.tract[,.(households=sum(households)),by=.(tract)]
hh.all.bg.tract <- merge(x=share.bg.tract,y=hh.all.tract,
                         by.x="GEOIDtract",by.y="tract") %>%
  select(GEOIDtract,GEOIDbg,share,hh_tract=households)
hh.all.bg.tract[,hh_bg:=hh_tract*share]
fwrite(hh.all.bg.tract,file='data/mobility data/EV Toolbox/allhh_tract_bg.csv',row.names=FALSE)

# get mapping from bg to TAZ
map.bg.TAZ <- fread('data/mapping/census bg to tract 2010/map bg to TAZ 2010.csv') %>%
  select(GEOIDbg=GEOID,TAZ=TAZ_Zone)

# calculate EV household share in each TAZ
hh.bg <- merge(x=hh.ev.bg.tract[,c("year","GEOIDbg","EVhh_bg")],y=hh.all.bg.tract[,c("GEOIDbg","hh_bg")])
hh.bg.TAZ <- merge(x=hh.bg,y=map.bg.TAZ,by="GEOIDbg")
hh.TAZ <- hh.bg.TAZ[,.(EVhh_TAZ=sum(EVhh_bg),hh_TAZ=sum(hh_bg)),by=.(year,TAZ)]
hh.TAZ[,EVhh_share:=EVhh_TAZ/hh_TAZ]
hh.TAZ[hh_TAZ==0,EVhh_share:=0]
hh.TAZ[EVhh_share>1,EVhh_share:=1]
fwrite(hh.TAZ,file='data/mobility data/EV Toolbox/evhh_share_TAZ.csv',row.names=FALSE)

