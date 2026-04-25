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
head(feeder.overload)

EV.grid[,tot_cap_share:=(EVload+baseload)/capacity]
head(EV.grid)

EV.grid <- merge(x=EV.grid,y=feeder.overload,by=c("FeederID","year"))
totaload.cap.share <- EV.grid[,.(tot_cap_share=max(tot_cap_share)),by=.(FeederID,year,overload_cat)]

EV.grid <- merge(EV.grid,feeder.type,by=c("FeederID","year"))
head(EV.grid)

max.overload <- EV.grid[overload_cat=='EV Overload',.(overload_max=max(overload)),by=.(FeederID,year,utility,max_type)]

cost.upgrade <- data.table(need_floor=c(0,1000,2000,4000,8000),
                           need_ceil=c(1000,2000,4000,8000,Inf),
                           min=c(37.04,9.9,30.3,78.15,73.91),
                           p25=c(445.71,251.84,196.76,268.65,237.23),
                           median=c(1875,1368.89,673.35,438.14,367.85),
                           p75=c(5791.67,2092.89,1447.55,785.3,586.32),
                           max=c(383900,5239.73,5633.51,3217.95,1267.76))

cost.overload <- cost.upgrade[max.overload,on=.(need_floor<=overload_max,need_ceil>overload_max)]
# need_floor and need_ceil become the same as overload_max afterwards
cost.overload[,cost_min:=min*need_ceil]
cost.overload[,cost_p25:=p25*need_ceil]
cost.overload[,cost_median:=median*need_ceil]
cost.overload[,cost_p75:=p75*need_ceil]
cost.overload[,cost_max:=max*need_ceil]

cost.utility <- cost.overload[,.(cost_min_tot=sum(cost_min),
                                 cost_p25_tot=sum(cost_p25),
                                 cost_median_tot=sum(cost_median),
                                 cost_p75_tot=sum(cost_p75),
                                 cost_max_tot=sum(cost_max)),
                              by=.(year,utility)]

cost.type <- cost.overload[,.(cost_min_tot=sum(cost_min),
                                 cost_p25_tot=sum(cost_p25),
                                 cost_median_tot=sum(cost_median),
                                 cost_p75_tot=sum(cost_p75),
                                 cost_max_tot=sum(cost_max)),
                              by=.(year,max_type)]

cost.all <- cost.overload[,.(cost_min_tot=sum(cost_min),
                                 cost_p25_tot=sum(cost_p25),
                                 cost_median_tot=sum(cost_median),
                                 cost_p75_tot=sum(cost_p75),
                                 cost_max_tot=sum(cost_max)),
                              by=.(year)]
setorder(cost.all,year)
setorder(cost.utility,year,utility)
setorder(cost.type,year,max_type)

plotSave <- ggplot(data=cost.utility)+
  geom_ribbon(aes(x=year,
              y=cost_median_tot/1000000000,
              ymin=cost_p25_tot/1000000000, 
              ymax=cost_p75_tot/1000000000,
              fill=utility),
              alpha=0.2) +
  #scale_fill_brewer(palette = "Paired")+
  geom_line(aes(x=year,y=cost_median_tot/1000000000,color=utility),size=1)+
  #scale_color_brewer(palette = "Paired")+
  xlab('Year')+
  ylab('Total Upgrade Cost ($B)')+
  labs(fill="Utility", color="Utility")+
  theme_bw()+
  theme(text = element_text(size = 15),
        panel.grid.major = element_blank(),
        panel.grid.minor = element_blank(),
        legend.position="bottom")
ggsave(plotSave,file=paste0('figures/PNAS/cost_utility.png'),height=4.3,width=4.3,dpi=500)

# cost.utility[utility=='PGE',population:=16000000]
# cost.utility[utility=='SCE',population:=15000000]
# cost.utility[utility=='SDGE',population:=3700000]

cost.utility[utility=='PGE',population:=59567564]
cost.utility[utility=='SCE',population:=61987760]
cost.utility[utility=='SDGE',population:=9707765]

cost.utility[,median_pcp:=cost_median_tot/population]
cost.utility[,p75_pcp:=cost_p75_tot/population]
cost.utility[,p25_pcp:=cost_p25_tot/population]

plotSave <- ggplot(data=cost.utility)+
  geom_ribbon(aes(x=year,
                  y=median_pcp,
                  ymin=p25_pcp, 
                  ymax=p75_pcp,
                  fill=utility),
              alpha=0.2) +
  #scale_fill_brewer(palette = "Paired")+
  geom_line(aes(x=year,y=median_pcp,color=utility),size=1)+
  #scale_color_brewer(palette = "Paired")+
  xlab('Year')+
  #ylab('Upgrade Cost Per Capita ($)')+
  ylab('Upgrade Cost Per Customer ($)')+
  labs(fill="Utility", color="Utility")+
  theme_bw()+
  theme(text = element_text(size = 15),
        panel.grid.major = element_blank(),
        panel.grid.minor = element_blank(),
        legend.position="bottom")
ggsave(plotSave,file=paste0('figures/PNAS/cost_utility_per customer.png'),height=4.3,width=4.3,dpi=500)


# plotSave <- ggplot(data=cost.type)+
#   geom_ribbon(aes(x=year,
#                   y=cost_median_tot/1000000000,
#                   ymin=cost_p25_tot/1000000000, 
#                   ymax=cost_p75_tot/1000000000,
#                   fill=max_type),
#               alpha=0.2) +
#   #scale_fill_brewer(palette = "Paired")+
#   geom_line(aes(x=year,y=cost_median_tot/1000000000,color=max_type),size=1)+
#   #scale_color_brewer(palette = "Paired")+
#   xlab('Year')+
#   ylab('Total Upgrade Cost ($B)')+
#   theme_bw()+
#   labs(fill="Feeder Type", color="Feeder Type")+
#   theme(text = element_text(size = 15),
#         panel.grid.major = element_blank(),
#         panel.grid.minor = element_blank(),
#         legend.position="bottom")
# ggsave(plotSave,file=paste0('figures/NE/cost_type.png'),height=4.3,width=4.3,dpi=500)

