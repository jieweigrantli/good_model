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

################### share of overload feeders over the years #####################
feeder.utility <- unique(EV.grid[,c("FeederID","utility")])
feeder.overload <- merge(feeder.overload,feeder.utility,by="FeederID")

overload.count.all <- feeder.overload[,.(count=.N),by=.(year,overload_cat)]
overload.count.all[,total:=sum(count),by=year]
overload.count.all[,share:=count/total]

overload.count <- feeder.overload[,.(count=.N),by=.(year,overload_cat,utility)]
overload.count[,total:=sum(count),by=year]
overload.count[,share:=count/total]

overload.count01 <- feeder.overload[,.(count=.N),by=.(year,overload01,utility)]
overload.count01[,total:=sum(count),by=year]
overload.count01[,share:=count/total]

overload.count.stats <- overload.count[overload_cat=='EV Overload']
setorder(overload.count.stats,year,utility)

plotSave <- ggplot()+
  geom_area(data=overload.count[overload_cat=='EV Overload'],aes(x=year,y=share,fill=utility))+
  xlab('Year')+
  ylab('Fraction of Overloaded Feeders')+
  labs(fill="Utility")+
  ylim(c(0,1))+
  #scale_fill_brewer(palette = "Paired")+
  theme_bw()+
  theme(text = element_text(size = 15),
        panel.grid.major = element_blank(),
        panel.grid.minor = element_blank(),
        legend.position="bottom")
ggsave(plotSave,file=paste0('figures/PNAS/overload_share_year_EV.png'),height=4.3,width=4.3,dpi=500)


################### share of overload feeders over the years by load type #####################
feeder.overload <- merge(feeder.overload,feeder.type,by=c("FeederID","year"))

overload.count <- feeder.overload[,.(count=.N),by=.(year,overload_cat,max_type)]
overload.count[,total:=sum(count),by=year]
overload.count[,share:=count/total]

overload.count.stats <- overload.count[overload_cat=='EV Overload']
setorder(overload.count.stats,year,max_type)

plotSave <- ggplot()+
  geom_area(data=overload.count[overload_cat=='EV Overload'],aes(x=year,y=share,fill=max_type))+
  xlab('Year')+
  ylab('Fraction of Overloaded Feeders')+
  labs(fill="Feeder Type")+
  ylim(c(0,1))+
  theme_bw()+
  theme(text = element_text(size = 15),
        panel.grid.major = element_blank(),
        panel.grid.minor = element_blank(),
        legend.position="bottom")
ggsave(plotSave,file=paste0('figures/NE/overload_share_year_EV_type.png'),height=4.3,width=4.3,dpi=500)

check.overload.count <- dcast(overload.count[overload_cat=='EV Overload'], year ~ max_type, value.var = "share")
check.overload.count[,ratio:=home/public]
