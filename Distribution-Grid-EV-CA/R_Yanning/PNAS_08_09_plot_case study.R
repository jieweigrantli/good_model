library(data.table)
library(sf)
library(ggplot2)
library(dplyr)
library(ggnewscale)

EV.grid <- readRDS('data/result/overload/overload_sample42_draw12_split6699_sample36.rds')
feeder.type <- fread('data/result/HWP/HWP max_sample42_draw12_split6699_sample36.rds')
feeder.HWP <- fread('data/result/HWP/HWP share_sample42_draw12_split6699_sample36.rds')
EV <- readRDS('data/result/charging profile by feeder/profile_feeder_cum_bins_IOU_sample42_draw12_split6699_sample36.rds')

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

EV.grid <- merge(x=EV.grid,y=feeder.overload,by=c("FeederID","year"))
overload.cap.share <- EV.grid[overload_cat=='EV Overload',.(over_cap_share=max(over_cap_share)),by=.(FeederID,year)]
EV.headroom.share <- EV.grid[overload_cat=='No Overload',.(EV_head_share=max(EV_head_share)),by=.(FeederID,year)]

EV.grid[,totload:=EVload+baseload]

frequency <- EV.grid[,.(overload_hour=sum(overload>0),all_hour=.N),by=.(FeederID,year)]
frequency[,freq:=overload_hour/all_hour]
frequency <- merge(frequency,feeder.type,by=c("FeederID","year"))

head(EV.grid)
head(EV)

avg.load <- EV.grid[,.(avg_baseload=mean(baseload),avg_ev=mean(EVload),
                       max_baseload=max(baseload),max_ev=max(EVload)),
                    by=.(FeederID,year)]
avg.load[,avg_ratio:=avg_ev/avg_baseload]
avg.load[,max_ratio:=max_ev/max_baseload]
frequency <- merge(frequency,avg.load[,c("FeederID","year","avg_ratio","max_ratio")],by=c("FeederID","year"))

peak.hour <- EV.grid[,.(EV_peak=hour[which.max(EVload)],
                        base_peak=hour[which.max(baseload)],
                        tot_peak=hour[which.max(totload)]),
                     by=.(FeederID,year,month)]


plot.case <- function(feeder,year,month,title){
  feeder.EV <- EV[FeederID==feeder.name & year==yr]
  feeder.grid <- EV.grid[FeederID==feeder.name & year==yr & month==m]
  feeder.grid <- feeder.grid[,c("FeederID",'hour','baseload','headroom','capacity','totload')]
  feeder.grid <- melt(feeder.grid, id.vars = c('FeederID','hour'),
                      measure.vars = c('baseload','headroom','capacity','totload'))
  setnames(feeder.EV,"charge_type","variable")
  setnames(feeder.EV,"load","value")
  feeder.EV[,year:=NULL]
  feeder.EV.grid <- rbind(feeder.EV,feeder.grid)
  
  plotsave <- ggplot()+
    geom_area(data=feeder.EV.grid[variable=='home'|variable=='public'|variable=='work'],
              aes(x=hour,y=value/1000,fill=variable))+
    geom_line(data=feeder.EV.grid[variable=='baseload'|variable=='headroom'],
              aes(x=hour,y=value/1000,linetype=variable))+
    ggtitle(title)+
    xlab('Hour')+
    ylab('MW')+
    labs(fill = "EV Charging\nLocation", linetype="Grid")+
    scale_x_continuous(breaks = seq(0, 24, by = 6))+
    theme_bw()+
    theme(text = element_text(size = 15),
          title = element_text(size=13),
          panel.grid.major = element_blank(),
          panel.grid.minor = element_blank())
  ggsave(plotsave,file=paste0('figures/PNAS/case_',title,'_',feeder.name,'_',yr,'_',m,'.png'),height=4,width=6,dpi=500)
  
}

##### home ########
yr <- 2045
candidate <- feeder.HWP[charge_type=='home'&HWP_share>0.7&year==yr]
can.EV <- feeder.overload[FeederID%in%candidate$FeederID & year==yr & overload_cat=="EV Overload"]
can.freq <- frequency[FeederID%in%can.EV$FeederID 
                      & year==yr 
                      & freq>0.3 & freq<0.7]
can.ratio <- can.freq[max_ratio<2 & all_hour==288]
setorder(can.ratio,max_ratio)
can.ratio <- can.ratio[,c("FeederID","year","freq","avg_ratio","max_ratio")]
can.peak <- peak.hour[FeederID%in%can.ratio$FeederID 
                      & year==yr
                      & base_peak>17]
can.peak <- merge(can.peak,can.ratio,by=c("FeederID","year"))
setorder(can.peak,max_ratio,base_peak)

# Pilot Hill
title <- 'Home Charging Dominated Feeder'
feeder.name <- '152282101'
m <- 8

plot.case(feeder=feeder.name,year=yr,month=m,title=title)

#### public #######
yr <- 2045

candidate <- feeder.HWP[charge_type=='public'&HWP_share>0.7&year==yr]
can.EV <- feeder.overload[FeederID%in%candidate$FeederID & year==yr & overload_cat=="EV Overload"]
can.freq <- frequency[FeederID%in%can.EV$FeederID 
                      & year==yr 
                      & freq>0.3 & freq<0.7]
can.ratio <- can.freq[max_ratio<2 & all_hour==288]
setorder(can.ratio,max_ratio)
can.ratio <- can.ratio[,c("FeederID","year","freq","avg_ratio","max_ratio")]
can.peak <- peak.hour[FeederID%in%can.ratio$FeederID 
                      & year==yr
                      & base_peak<17]
can.peak <- merge(can.peak,can.ratio,by=c("FeederID","year"))
setorder(can.peak,max_ratio,base_peak)

# Escondido
title <- 'Public Charging Dominated Feeder'
feeder.name <- '515'
m <- 8

plot.case(feeder=feeder.name,year=yr,month=m,title=title)








