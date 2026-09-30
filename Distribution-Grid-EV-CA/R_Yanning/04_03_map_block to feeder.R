library(sf)
library(tidyverse)
library(data.table)

############## combine the feeders in 3 utilities ##############################
PGE.feeders <- read_sf("data/mapping/block to feeder 2010/shape all/PGE feeders.shp")
# from map, not the version from them
SCE.feeders <- read_sf("data/mapping/block to feeder 2010/shape all/SCE circuits.shp")
# lines
SDGE.feeders <- read_sf("data/mapping/block to feeder 2010/shape SDGE lines merge/SDGE Feeder lines.shp")

# transform all projections to 4326
PGE.feeders <- st_transform(PGE.feeders,crs = st_crs(SCE.feeders))
SDGE.feeders <- st_transform(SDGE.feeders,crs = st_crs(SCE.feeders))

PGE.feeders.short <- PGE.feeders[,c("FeederID")]
SCE.feeders.short <- SCE.feeders[,c("CIRCUIT_NA")]
SDGE.feeders.short <- SDGE.feeders[,c("FEEDERID")]

SDGE.feeders.short = SDGE.feeders.short%>% add_column(utility="SDGE")
SCE.feeders.short = SCE.feeders.short%>% add_column(utility="SCE")
PGE.feeders.short = PGE.feeders.short%>% add_column(utility="PGE")

setnames(SCE.feeders.short,"CIRCUIT_NA","FeederID")
setnames(SDGE.feeders.short,"FEEDERID","FeederID")

feeders.all <- rbind(SDGE.feeders.short,SCE.feeders.short)
feeders.all <- rbind(feeders.all,PGE.feeders.short)

st_write(feeders.all,"data/mapping/block to feeder 2010/shape all/all feeders_map_line.shp")

############# join: assign blocks to feeders ####################################
feeders.all <- read_sf("data/mapping/block to feeder 2010/shape all/all feeders_map_line.shp")

block <- read_sf("data/mapping/census block to TAZ 2010/shape_block_2010/tl_2019_06_tabblock10.shp")
block <- st_transform(block,crs = st_crs(feeders.all))
block.short <- block[,c("GEOID10")]

## 1. assign each block to the nearest feeder 
start.time <- Sys.time()
block.to.feeder.nearest <- st_join(block.short,feeders.all,join=st_nearest_feature) 
end.time <- Sys.time()
end.time - start.time
st_write(block.to.feeder.nearest,"data/mapping/block to feeder 2010/shape mapped/block 2010 to feeder_map_line_nearest.shp")

# check if there are any feeder not assigned to any block
block.to.feeder.nearest <- read_sf("data/mapping/block to feeder 2010/shape mapped/block 2010 to feeder_map_line_nearest.shp")
length(unique(block.to.feeder.nearest$FeederID)) # 8018/8158 - 140 unassigned
feeders.assigned <- unique(block.to.feeder.nearest$FeederID)
setdiff(feeders.all$FeederID,feeders.assigned)

## 2. try the max intersection method in QGIS
block.to.feeder.intersect <- read_sf("data/mapping/block to feeder 2010/shape mapped/block 2010 to feeder_map_line_intersect.shp")
block.to.feeder.intersect <- block.to.feeder.intersect[,c("GEOID10","FeederID","utility")]
nrow(filter(block.to.feeder.intersect,is.na(FeederID)))

# the blocks that actually intersect with feeders
intersect.blocks <- filter(block.to.feeder.intersect,!is.na(FeederID))
# compare the valid assigned feeders between intersect & nearest
intersect.feeders <- unique(intersect.blocks$FeederID) # 8011/8158
nearest.feeders <- unique(block.to.feeder.nearest$FeederID) # 8018/8158
setdiff(intersect.feeders,nearest.feeders)
setdiff(nearest.feeders,intersect.feeders)

# the blocks that don't intersect with feeders: use nearest
nearest.blocks.rest <- filter(block.to.feeder.nearest,!GEOID10%in%intersect.blocks$GEOID10)
intersect.blocks <- st_transform(intersect.blocks,crs = st_crs(nearest.blocks.rest))
block.to.feeder.combo <- rbind(intersect.blocks,nearest.blocks.rest)

st_write(block.to.feeder.combo,"data/mapping/block to feeder 2010/shape mapped/block 2010 to feeder_map_line_combo.shp")

# unassigned feeders
length(unique(feeders.all$FeederID)) # 8156
nrow(unique(feeders.all)) # 8156
length(unique(block.to.feeder.combo$FeederID)) # 8032/8156 slightly more
setdiff(feeders.all$FeederID,unique(block.to.feeder.combo$FeederID)) # Mostly SDGE (maybe the new ones)
feeders.unassigned <- filter(feeders.all,FeederID%in%setdiff(feeders.all$FeederID,unique(block.to.feeder.combo$FeederID)))
st_write(feeders.unassigned,"data/mapping/block to feeder 2010/shape mapped/feeder_unassigned_map_line_combo.shp")
