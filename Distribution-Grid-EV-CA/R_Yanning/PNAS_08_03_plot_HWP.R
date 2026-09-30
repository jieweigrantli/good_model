library(data.table)
library(sf)
library(ggplot2)
library(dplyr)

#EV.grid <- readRDS('data/result/overload/overload_sample42_draw12_split6699_sample36.rds')
EV <- readRDS('data/result/charging profile by feeder/profile_feeder_cum_bins_IOU_sample42_draw12_split6699_sample36.rds')

IOU <- read_sf("data/mapping/block to feeder 2010/shape IOUs++/shape IOUs++.shp")
county <- read_sf('data/mapping/county tract 2010/tl_2018_us_county/tl_2018_us_county.shp')
county.CA <- filter(county,STATEFP=='06')

feeders.all <- read_sf("data/mapping/block to feeder 2010/shape all/all feeders_map_line.shp")

head(EV)
feeder.HWP <- EV[,.(tot_load_type=sum(load)),by=.(FeederID,charge_type,year)]
feeder.HWP[,tot_load:=sum(tot_load_type),by=.(FeederID,year)]
feeder.HWP[,HWP_share:=tot_load_type/tot_load]

fwrite(feeder.HWP,'data/result/HWP/HWP share_sample42_draw12_split6699_sample36.rds')

feeder.HWP.cat <- feeder.HWP[, .(max_type = charge_type[which.max(HWP_share)]), by =.(FeederID,year)]
nrow(feeder.HWP.cat[max_type=='work'&year==2045])

fwrite(feeder.HWP.cat,'data/result/HWP/HWP max_sample42_draw12_split6699_sample36.rds')

feeder.HWP.cat.sf <- merge(feeders.all,feeder.HWP.cat,by='FeederID')

check.HWP.energy <- EV[,.(tot_load_type=sum(load)),by=.(charge_type,year)]
check.HWP.energy[,tot_load:=sum(tot_load_type),by=.(year)]
check.HWP.energy[,HWP_share:=tot_load_type/tot_load]

check.HWP.feeder <- feeder.HWP.cat[,.(feeder_count = .N),by=.(max_type,year)]
check.HWP.feeder[,tot_count:=sum(feeder_count),by=.(year)]
check.HWP.feeder[,HWP_share:=feeder_count/tot_count]

for (yr in c(2045)){
  plotSave <- ggplot()+
    geom_sf(data = IOU,
            color=NA,
            fill="grey")+
    geom_sf(data=county.CA,
            color="white",
            fill='grey84',alpha=0.5)+
    geom_sf(data = filter(feeder.HWP.cat.sf,year==yr), aes(colour = max_type))+
    labs(color='EV Load Type that Dominates this Feeder')+
    ggtitle(yr)+
    theme_bw()+
    theme(legend.position="bottom")+
    theme(text = element_text(size = 20),
          panel.grid.major = element_blank(),
          panel.grid.minor = element_blank())
  ggsave(plotSave,file=paste0('figures/PNAS/HWP_category_',yr,'.png'),height=10,width=10,dpi=500)
}

###### pie chart of charging demand #####################################################
plotSave <- ggplot(data=check.HWP.energy[year==2045],aes(x="", y=HWP_share, fill=charge_type))+
  geom_bar(stat="identity", width=1, color="white") +
  coord_polar("y", start=0) +
  labs(fill='Charging Location')+
  ggtitle("Share of Charging Demand")+
  geom_text(aes(label = scales::percent(HWP_share)),
            position = position_stack(vjust = 0.5),
            size=8,color="white") +
  theme_void()+
  theme(legend.position="bottom")+
  theme(text = element_text(size = 20),
        panel.grid.major = element_blank(),
        panel.grid.minor = element_blank())
ggsave(plotSave,file=paste0('figures/PNAS/HWP_charging demand.png'),height=5,width=5,dpi=500)

###### pie chart of all trips #####################################################
trip.sd <- readRDS('data/mobility data/CSTDM processed/SDPTM trips short.rds')
trip.ld <- fread('C:/Nina/Research (large files)/CSTDM/LDPTM/LDPTM_Trips.csv')
trip.ext <- fread('C:/Nina/Research (large files)/CSTDM/ETM/trips_Ext.csv')

load.files <- function(directory) {
  files.all <- list.files(path=directory)
  output <- data.table()
  for(f in files.all) {
    hold <- fread(paste0(directory,'/',f))
    output <- rbind(output,hold)
  }
  return(output)
}
TAZ.dist.ETM <- load.files(directory='data/mobility data/TAZ distance/parsed_100000_ETM')
TAZ.dist.ETM.short <- TAZ.dist.ETM[,c("from","to","distance")]
TAZ.dist.ETM.short <- unique(TAZ.dist.ETM.short)

trip.sd <- trip.sd[(Mode=='SOV'|Mode=='HOV2'|Mode=='HOV3'),
                   c("DPurp","Dist")]
aggregate.purpose <- data.table('ChargeType'=c('H','W','W','P','P','P','P','P','P','P','P'),
                                'DPurp'=c('O','W','P','S','H','T','C','L','R','K','Z'))
trip.sd <- merge(x=trip.sd,y=aggregate.purpose,by='DPurp')


trip.ld <- trip.ld[(Mode=='SOV'|Mode=='HOV2'|Mode=='HOV3'),
                   c("DPurp","Dist")]
aggregate.purpose <- data.table('ChargeType'=c('W','H','P','W','P'),
                                'DPurp'=c('Bus','VFR','Rec','Com','Oth'))
trip.ld <- merge(x=trip.ld,y=aggregate.purpose,by='DPurp')


trip.ext <- trip.ext[(ActorType=='CarLong'|ActorType=='CarLocal')
                     & (Mode=='SOV'|Mode=='HOV2'|Mode=='HOV3'),
                     c("DPurp","I","J")]
trip.ext[,ChargeType:="P"]
trip.ext <- merge(x=trip.ext,y=TAZ.dist.ETM.short,
                  by.x=c("I","J"),by.y=c("from","to"))
trip.ext[,Dist:=distance/1609.344]
trip.ext <- trip.ext[,c("DPurp","Dist","ChargeType")]

trip.all <- rbind(trip.sd,trip.ld,trip.ext)
trip.HWP <- trip.all[,.(count=.N, distance=sum(Dist)),by=.(ChargeType)]
trip.HWP[ChargeType=='H', ChargeType:='Home']
trip.HWP[ChargeType=='P', ChargeType:='Public']
trip.HWP[ChargeType=='W', ChargeType:='Work']
trip.HWP[,distance_share:=distance/sum(distance)]

plotSave <- ggplot(data=trip.HWP,aes(x="", y=distance_share, fill=ChargeType))+
  geom_bar(data=trip.HWP, aes(x="", y=distance_share, fill=ChargeType),
           stat="identity", width=1, color="white") +
  coord_polar("y", start=0) +
  labs(fill='Trip Purpose')+
  ggtitle("Share of Miles Driven")+
  geom_text(aes(label = scales::percent(distance_share)),
            position = position_stack(vjust = 0.5),
            size=8,color="white") +
  theme_void()+
  theme(legend.position="bottom")+
  theme(text = element_text(size = 20),
        panel.grid.major = element_blank(),
        panel.grid.minor = element_blank())
ggsave(plotSave,file=paste0('figures/PNAS/HWP_all trip distance.png'),height=5,width=5,dpi=500)
