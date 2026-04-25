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

############## overload frequency by HWP #########################################
frequency <- EV.grid[,.(overload_hour=sum(overload>0),all_hour=.N),by=.(FeederID,year)]
frequency[,freq:=overload_hour/all_hour]
frequency <- merge(frequency,feeder.type,by=c("FeederID","year"))
unique(feeder.type[year==2045]$max_type)

for (yr in c(2045)){
  plotSave <- ggplot()+
    geom_freqpoly(data=frequency[year==yr&freq>0],
                  aes(freq,colour=max_type,after_stat(density)),
                  size=0.7,
                  position="identity",
                  binwidth = 0.025)+
    ggtitle(paste0(" Overload Frequency"))+
    labs(colour='EV Load Type that Dominates this Feeder')+
    xlab('Overload Hours/All Hours')+
    ylab('Density of Count of Feeders')+
    theme_bw()+
    theme(legend.position="bottom")+
    theme(text = element_text(size = 15))+
    theme(legend.title = element_text(size=13),
          panel.grid.major = element_blank(),
          panel.grid.minor = element_blank())
  ggsave(plotSave,file=paste0(paste0('figures/PNAS/freq_pdf_HWP_',yr,'.png')),height=4,width=6,dpi=500)
}

