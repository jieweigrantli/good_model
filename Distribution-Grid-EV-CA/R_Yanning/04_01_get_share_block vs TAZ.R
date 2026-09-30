library(data.table)
library(tidyverse)

map.block.TAZ <- fread('data/mapping/census block to TAZ 2010/map census block to TAZ 2010.csv') %>%
  select(GEOID10,TAZ_Zone)
summary(map.block.TAZ)
map.block.TAZ[,GEOID10:=as.character(GEOID10)]

pop.block <- fread('data/mapping/census block to TAZ 2010/census block population_2010_short.csv')
summary(pop.block)
pop.block[,GEOID10:=substr(GEOID, nchar(GEOID)-14+1,nchar(GEOID))]
pop.block[,population_block:=as.numeric(population)]
#pop.block.na <- pop.block[is.na(population.block)]
#pop.block.na[,population.block:=as.numeric(substr(population,1,nchar(population)-8))]
pop.block[is.na(population_block),population_block:=as.numeric(substr(population,1,nchar(population)-8))]
summary(pop.block)

job.block <- fread('data/mapping/census block to TAZ 2010/LODES/ca_wac_S000_JT00_2019.csv.gz') %>%
  select(w_geocode,C000)
summary(job.block)
setnames(job.block,"w_geocode","GEOID10")
setnames(job.block,"C000","jobs_block")
job.block[,GEOID10:=as.character(GEOID10)]

block.TAZ.share <- merge(x=map.block.TAZ,y=pop.block,by="GEOID10")
block.TAZ.share <- merge(x=block.TAZ.share,y=job.block,by="GEOID10",all.x=T)
block.TAZ.share <- block.TAZ.share[,c("GEOID10","TAZ_Zone","population_block","jobs_block")]
# set number of jobs to 0 at blocks not recorded in "workplace area characteristics"
block.TAZ.share[is.na(jobs_block),jobs_block:=0]
# consider only the blocks that are within TAZs - 1322 blocks not in TAZs
block.TAZ.share <- block.TAZ.share[!is.na(TAZ_Zone)]
# calculate the share of block vs TAZ
block.TAZ.share[,population_TAZ:=sum(population_block),by=.(TAZ_Zone)]
block.TAZ.share[,jobs_TAZ:=sum(jobs_block),by=.(TAZ_Zone)]
block.TAZ.share[,population_share:=population_block/population_TAZ]
block.TAZ.share[,jobs_share:=jobs_block/jobs_TAZ]
nrow(block.TAZ.share[population_TAZ==0]) #1396
nrow(block.TAZ.share[jobs_TAZ==0]) #737
block.TAZ.share[population_TAZ==0,population_share:=0]
block.TAZ.share[jobs_TAZ==0,jobs_share:=0]
summary(block.TAZ.share)

fwrite(block.TAZ.share,file='data/mapping/census block to TAZ 2010/share_block vs TAZ.csv',row.names=FALSE)

