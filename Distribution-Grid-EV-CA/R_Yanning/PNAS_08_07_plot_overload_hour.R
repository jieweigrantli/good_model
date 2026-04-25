library(data.table)
library(sf)
library(ggplot2)
library(dplyr)
library(ggnewscale)

EV.grid <- readRDS('data/result/overload/overload_sample42_draw12_split6699_sample36.rds')
feeder.type <- fread('data/result/HWP/HWP max_sample42_draw12_split6699_sample36.rds')

IOU <- read_sf("data/mapping/block to feeder 2010/shape IOUs++/shape IOUs++.shp")
county <- read_sf('data/mapping/county tract 2010/tl_2018_us_county/tl_2018_us_county.shp')
county.CA <- filter(county,STATEFP=='06')

feeders.all <- read_sf("data/mapping/block to feeder 2010/shape all/all feeders_map_line.shp")

head(EV.grid)
EV.grid[,season:='Spring'] 
EV.grid[month %in% c(6,7,8),season:='Summer'] 
EV.grid[month %in% c(9,10,11),season:='Fall'] 
EV.grid[month %in% c(12,1,2),season:='Winter']

feeder.overload <- EV.grid[,.(overload01=any(overload>0)),by=.(FeederID,year)]
feeder.natural.overload <- unique(EV.grid[year==2022&headroom<=0]$FeederID)
feeder.overload[,overload_cat:='No Overload']
feeder.overload[overload01=='TRUE',overload_cat:='EV Overload']
feeder.overload[FeederID%in%feeder.natural.overload,overload_cat:='Baseload Overload']

EV.grid[,over_cap_share:=overload/capacity]
EV.grid[,EV_head_share:=EVload/headroom]

EV.grid <- merge(EV.grid,feeder.type,by=c("FeederID","year"))
head(EV.grid)

####################### overload hour distribution: HWP ####################################
hour.overload.type <- EV.grid[,.(overload_feeder=sum(overload>0)),by=.(hour,year,max_type)]

for (yr in c(2045)){
  plotSave <- ggplot()+
    geom_freqpoly(data=EV.grid[year==yr&overload>0],
                   aes(hour,colour=max_type,y=after_stat(density)),
                   size=0.7,
                   position="identity",
                   binwidth=1)+
    scale_x_continuous(breaks = seq(0, 24, by = 6))+
    xlab('Overload Hour')+
    ylab('Density')+
    labs(colour='EV Load Type that Dominates on this Feeder')+
    #ylim(c(0,1))+
    theme_bw()+
    theme(legend.position="bottom")+
    theme(text = element_text(size = 15))+
    theme(legend.title = element_text(size=13),
          panel.grid.major = element_blank(),
          panel.grid.minor = element_blank())
  ggsave(plotSave,file=paste0('figures/PNAS/hour_density_HWP_',yr,'.png'),height=4,width=6.5,dpi=500)
  ggsave(plotSave,file=paste0('figures/PNAS/TIFF/hour_density_HWP_',yr,'.tiff'),height=4,width=6.5,dpi=500)
}

# for (yr in c(2045)){
#   plotSave <- ggplot()+
#     geom_density(data=EV.grid[year==yr&overload>0],aes(hour,colour=max_type,fill=max_type),
#                  alpha=0.1)+
#     ggtitle(yr)+
#     xlab('Overload Hour')+
#     ylab('Density')+
#     labs(fill='EV Load Type that Dominates on this Feeder',colour='EV Load Type that Dominates on this Feeder')+
#     #ylim(c(0,1))+
#     theme_bw()+
#     theme(legend.position="bottom")+
#     theme(text = element_text(size = 15))+
#     theme(legend.title = element_text(size=13),
#           panel.grid.major = element_blank(),
#           panel.grid.minor = element_blank())
#   ggsave(plotSave,file=paste0('figures/PNAS/hour_density_HWP_',yr,'.png'),height=4,width=6,dpi=500)
#   ggsave(plotSave,file=paste0('figures/PNAS/TIFF/hour_density_HWP_',yr,'.tiff'),height=4,width=6,dpi=500)
# }




