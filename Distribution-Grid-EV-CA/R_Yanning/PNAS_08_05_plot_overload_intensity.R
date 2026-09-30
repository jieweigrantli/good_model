library(data.table)
library(sf)
library(ggplot2)
library(dplyr)
library(ggnewscale)
library(RColorBrewer)

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

EV.grid[,tot_cap_share:=(EVload+baseload)/capacity]
head(EV.grid)

EV.grid <- merge(x=EV.grid,y=feeder.overload,by=c("FeederID","year"))
totaload.cap.share <- EV.grid[,.(tot_cap_share=max(tot_cap_share)),by=.(FeederID,year,overload_cat)]
overload.max <- EV.grid[,.(max_overload=max(overload)),by=.(FeederID,year,overload_cat)]

EV.grid <- merge(EV.grid,feeder.type,by=c("FeederID","year"))

##################### map of overload intensity ####################################
totaload.cap.share.sf <- merge(feeders.all,totaload.cap.share,by='FeederID')
totaload.cap.share[tot_cap_share==1]

for (yr in c(2025,2030,2035,2045)){
  plotSave <- ggplot()+
    ggtitle(paste0(yr))+
    geom_sf(data = IOU,
            color=NA,
            fill="grey")+
    geom_sf(data=county.CA,
            color='white',
            fill='grey84',alpha=0.5)+
    geom_sf(data = filter(totaload.cap.share.sf,year==yr & (overload_cat=='EV Overload'|overload_cat=='No Overload')), 
            aes(colour = tot_cap_share))+
    scale_color_stepsn(limits=c(0,2),
                       #show.limits=TRUE,
                       breaks=c(0.25,0.5,0.75,1,1.25,1.5,1.75),
                       colors = c("#B3C8E5","#8CA4D0","#6381BB",'#3461A6',
                                  '#F3A88E','#DF8169','#C95B46','#B22F25')) +
    labs(color='Total Load/Capacity')+
    theme_bw()+
    #theme(legend.position="bottom")+
    theme(text = element_text(size = 20),
          panel.grid.major = element_blank(),
          panel.grid.minor = element_blank(),
          legend.key.height= unit(2, 'cm'))
  ggsave(plotSave,file=paste0('figures/PNAS/Intensity_feeder_longkey_',yr,'.png'),height=10,width=10,dpi=500)
}

# for (yr in c(2025,2030,2035,2045)){
#   plotSave <- ggplot()+
#     ggtitle(paste0(yr))+
#     geom_sf(data = IOU,
#             color=NA,
#             fill="grey60")+
#     geom_sf(data=county.CA,
#             color=NA,
#             fill='grey',alpha=0.5)+
#     geom_sf(data = filter(totaload.cap.share.sf,year==yr & (overload_cat=='EV Overload'|overload_cat=='No Overload')), 
#             aes(colour = tot_cap_share))+
#     scale_color_stepsn(limits=c(0,2),
#                        #show.limits=TRUE,
#                        breaks=c(0.25,0.5,0.75,1,1.25,1.5,1.75),
#                        colors = c("#313695","#4575b4","#74add1",'#abd9e9',
#                                   '#fdae61','#f46d43','#d73027','#a50026')) +
#     labs(color='Total Load/Capacity')+
#     theme_bw()+
#     #theme(legend.position="bottom")+
#     theme(text = element_text(size = 20),
#           panel.grid.major = element_blank(),
#           panel.grid.minor = element_blank(),
#           legend.key.height= unit(2, 'cm'))
#   ggsave(plotSave,file=paste0('figures/PNAS/Intensity_feeder_1_',yr,'.png'),height=10,width=10,dpi=500)
# }


################# pdf of intensity:HWP ########################################
totaload.cap.share <- merge(totaload.cap.share,feeder.type,by=c("FeederID","year"))

ks.test(totaload.cap.share[year==2045&overload_cat=='EV Overload'&max_type=='home']$tot_cap_share,
        totaload.cap.share[year==2045&overload_cat=='EV Overload'&max_type=='public']$tot_cap_share)

nrow(totaload.cap.share[year==2045&overload_cat=='EV Overload'&max_type=='public'])
1.073*sqrt((nrow(totaload.cap.share[year==2045&overload_cat=='EV Overload'&max_type=='home'])+
       nrow(totaload.cap.share[year==2045&overload_cat=='EV Overload'&max_type=='public']))/
         ((nrow(totaload.cap.share[year==2045&overload_cat=='EV Overload'&max_type=='home'])*
             nrow(totaload.cap.share[year==2045&overload_cat=='EV Overload'&max_type=='public']))))
sqrt(-log(0.001/2)*(1+2448/1276)/(2*2448))

for (yr in c(2045)){
  plotSave <- ggplot()+
    geom_freqpoly(data=totaload.cap.share[year==yr&overload_cat=='EV Overload'],
                   aes(tot_cap_share,colour=max_type,y=after_stat(density)),
                   size=0.7,
                   position="identity",
                   binwidth = 0.1)+
    xlim(1,5)+
    ggtitle(paste0("Overload Intensity"))+
    labs(colour='EV Load Type that Dominates this Feeder')+
    xlab('Total Load/Capacity')+
    ylab('Density of Count of Feeders')+
    theme_bw()+
    theme(legend.position="bottom")+
    theme(text = element_text(size = 15))+
    theme(legend.title = element_text(size=13),
          panel.grid.major = element_blank(),
          panel.grid.minor = element_blank())
  ggsave(plotSave,file=paste0(paste0('figures/PNAS/tot_cap_pdf_HWP_',yr,'.png')),height=4,width=6,dpi=500)
}

# for (yr in c(2045)){
#   plotSave <- ggplot()+
#     geom_freqpoly(data=totaload.cap.share[year==yr&(overload_cat=='EV Overload'|overload_cat=='No Overload')],
#                   aes(tot_cap_share,colour=max_type,y=after_stat(density)),
#                   size=0.7,
#                   position="identity",
#                   binwidth = 0.1)+
#     xlim(0,5)+
#     geom_vline(xintercept=1, color='red')+
#     ggtitle(paste0("Overload Intensity"))+
#     labs(colour='EV Load Type that Dominates this Feeder')+
#     xlab('Total Load/Capacity')+
#     ylab('Density of Count of Feeders')+
#     theme_bw()+
#     theme(legend.position="bottom")+
#     theme(text = element_text(size = 15))+
#     theme(legend.title = element_text(size=13),
#           panel.grid.major = element_blank(),
#           panel.grid.minor = element_blank())
#   ggsave(plotSave,file=paste0(paste0('figures/PNAS/tot_cap_pdf_HWP_from0_',yr,'.png')),height=4,width=6,dpi=500)
# }

# for (yr in c(2045)){
#   plotSave <- ggplot()+
#     geom_density(data=totaload.cap.share[year==yr&overload_cat=='EV Overload'],
#                    aes(tot_cap_share,colour=max_type,fill=max_type),
#                    alpha=0.1,
#                    position="identity")+
#     xlim(1,5)+
#     ggtitle(paste0("Overload Intensity"))+
#     labs(fill='EV Load Type that Dominates this Feeder',colour='EV Load Type that Dominates this Feeder')+
#     xlab('Total Load/Capacity')+
#     ylab('Density of Count of Feeders')+
#     theme_bw()+
#     theme(legend.position="bottom")+
#     theme(text = element_text(size = 15))+
#     theme(legend.title = element_text(size=13),
#           panel.grid.major = element_blank(),
#           panel.grid.minor = element_blank())
#   ggsave(plotSave,file=paste0(paste0('figures/PNAS/tot_cap_pdf_HWP_',yr,'.png')),height=4,width=6,dpi=500)
# }

################# pdf of max overload value:HWP ########################################
overload.max <- merge(overload.max,feeder.type,by=c("FeederID","year"))
summary(overload.max[overload_cat=='EV Overload'])


for (yr in c(2045)){
  plotSave <- ggplot()+
    geom_freqpoly(data=overload.max[year==yr&overload_cat=='EV Overload'],
                  aes(max_overload,colour=max_type,y=after_stat(count)),
                  size=0.7,
                  position="identity",
                  binwidth = 100)+
    xlim(1,30000)+
    ggtitle(paste0("Overload Intensity"))+
    labs(colour='EV Load Type that Dominates this Feeder')+
    xlab('Overload Size (kW)')+
    ylab('Count of Feeders')+
    theme_bw()+
    theme(legend.position="bottom")+
    theme(text = element_text(size = 15))+
    theme(legend.title = element_text(size=13),
          panel.grid.major = element_blank(),
          panel.grid.minor = element_blank())
  ggsave(plotSave,file=paste0(paste0('figures/PNAS/overloadsize_count_HWP_',yr,'.png')),height=4,width=6,dpi=500)
}

for (yr in c(2045)){
  plotSave <- ggplot()+
    geom_freqpoly(data=overload.max[year==yr&overload_cat=='EV Overload'],
                  aes(max_overload,colour=max_type,y=after_stat(density)),
                  size=0.7,
                  position="identity",
                  binwidth = 1000)+
    #xlim(1,30000)+
    ggtitle(paste0("Overload Intensity"))+
    labs(colour='EV Load Type that Dominates this Feeder')+
    xlab('Overload Size (kW)')+
    ylab('Density of Count of Feeders')+
    theme_bw()+
    theme(legend.position="bottom")+
    theme(text = element_text(size = 15))+
    theme(legend.title = element_text(size=13),
          panel.grid.major = element_blank(),
          panel.grid.minor = element_blank())
  ggsave(plotSave,file=paste0(paste0('figures/PNAS/overloadsize_pdf_HWP_',yr,'.png')),height=4,width=6,dpi=500)
}

############# upgrade need by utility ##################################################
max.overload <- EV.grid[overload_cat=='EV Overload',.(overload_max=max(overload)),by=.(FeederID,year,utility)]
upgrade <- max.overload[,.(upgrade=sum(overload_max)),by=.(year,utility)]
upgrade[,upgrade_GW:=upgrade/1000000]
upgrade[,upgrade_MW:=upgrade/1000]
setorder(upgrade,year,utility)

upgrade.all <- max.overload[,.(upgrade=sum(overload_max)),by=.(year)]
upgrade.all[,upgrade_GW:=upgrade/1000000]
setorder(upgrade.all,year)

plotSave <- ggplot()+
  geom_area(data=upgrade,aes(x=year,y=upgrade_GW,fill=utility))+
  xlab('Year')+
  ylab('Required Capacity Upgrade (GW)')+
  labs(fill="Utility")+
  #scale_fill_brewer(palette = "Paired")+
  #ylim(c(0,1))+
  theme_bw()+
  #theme(legend.position="bottom")+
  theme(text = element_text(size = 15),
        panel.grid.major = element_blank(),
        panel.grid.minor = element_blank(),
        legend.position="bottom")
ggsave(plotSave,file=paste0('figures/PNAS/upgrade_total_year_area.png'),height=4.3,width=4.3,dpi=500)



############# upgrade need by feeder type ##################################################
# max.overload <- EV.grid[overload_cat=='EV Overload',.(overload_max=max(overload)),by=.(FeederID,year,max_type)]
# upgrade <- max.overload[,.(upgrade=sum(overload_max)),by=.(year,max_type)]
# upgrade[,upgrade_GW:=upgrade/1000000]
# upgrade[,upgrade_MW:=upgrade/1000]
# 
# setorder(upgrade,year,max_type)
# 
# plotSave <- ggplot()+
#   geom_area(data=upgrade,aes(x=year,y=upgrade_GW,fill=max_type))+
#   xlab('Year')+
#   ylab('Required Capacity Upgrade (GW)')+
#   labs(fill="Feeder Type")+
#   #ylim(c(0,1))+
#   theme_bw()+
#   #theme(legend.position="bottom")+
#   theme(text = element_text(size = 15),
#         panel.grid.major = element_blank(),
#         panel.grid.minor = element_blank(),
#         legend.position="bottom")
# ggsave(plotSave,file=paste0('figures/PNAS/upgrade_total_year_type.png'),height=4.3,width=4.3,dpi=500)
# 
# check.upgrade <- dcast(upgrade, year ~ max_type, value.var = "upgrade")
# check.upgrade[,ratio:=home/public]
