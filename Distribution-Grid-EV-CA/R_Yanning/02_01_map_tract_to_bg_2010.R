# this is part of script "02_01a" that are useful for the main codes
library(tidyverse)
library(data.table)


# tract population & block group population
tract.pop <- fread('data/mapping/census bg to tract 2010/Census Tract Population_2010_short.csv') # 8057
bg.pop <- fread('data/mapping/census bg to tract 2010/Census Block Group Population_2010_short.csv')

tract.pop[,GEOIDshort:=as.numeric(substr(GEOID,11,nchar(GEOID)))]
bg.pop[,GEOIDshort:=as.numeric(substr(GEOID,11,nchar(GEOID)))]

# mapping from tract to block group (2010-2019) - can be done by GEOID
bg.pop[,GEOIDtract:=as.numeric(substr(GEOID,11,nchar(GEOID)-1))]
setnames(bg.pop,"Population","bg_pop")
setnames(bg.pop,"GEOIDshort","GEOIDbg")
setnames(tract.pop,"Population","tract_pop")
setnames(tract.pop,"GEOIDshort","GEOIDtract")

pop.bg.tract <- merge(x=bg.pop[,c("GEOIDbg","GEOIDtract","bg_pop")],y=tract.pop[,c("GEOIDtract","tract_pop")],
                      by="GEOIDtract",all.x=T)

# distribute number of EVs from tracts to block groups
EV.tract.toolbox.acc2[,GEOID:=as.numeric(tract)]

pop.bg.tract[,share:=as.numeric(bg_pop)/as.numeric(tract_pop)]
summary(pop.bg.tract$share)
pop.bg.tract[is.na(share)]
pop.bg.tract[tract_pop=="0",share:=0]
pop.bg.tract[is.na(share)]
pop.bg.tract[is.na(share),tract_pop:=substr(tract_pop,1,nchar(tract_pop)-8)]
pop.bg.tract[is.na(share)]
pop.bg.tract[is.na(share),share:=as.numeric(bg_pop)/as.numeric(tract_pop)]
summary(pop.bg.tract$share)
tract.pop[nchar(tract_pop)>5]
fwrite(pop.bg.tract,file='data/mapping/census bg to tract 2010/popluation share bg to tract 2010.csv',row.names=FALSE)