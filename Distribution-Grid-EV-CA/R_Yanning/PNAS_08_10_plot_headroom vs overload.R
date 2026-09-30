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

feeder.overload <- EV.grid[,.(overload01=any(overload>0)),by=.(FeederID,year)]
feeder.natural.overload <- unique(EV.grid[year==2022&headroom<=0]$FeederID)
feeder.overload[,overload_cat:='No Overload']
feeder.overload[overload01=='TRUE',overload_cat:='EV Overload']
feeder.overload[FeederID%in%feeder.natural.overload,overload_cat:='Baseload Overload']

EV.grid[,tot_cap_share:=(EVload+baseload)/capacity]
head(EV.grid)

EV.grid <- merge(x=EV.grid,y=feeder.overload,by=c("FeederID","year"))
totaload.cap.share <- EV.grid[,.(tot_cap_share=max(tot_cap_share)),by=.(FeederID,year,overload_cat)]

EV.grid <- merge(EV.grid,feeder.type,by=c("FeederID","year"))

EV.grid[,headroom_share:=headroom/capacity]
feeder.vs <- EV.grid[,.(min_headroom_share=min(headroom_share),
                        max_overload_intensity=max(tot_cap_share)),
                     by=.(FeederID,year,overload_cat,max_type)]


colors <- c("2025 ( 8% EV)" = "#26294A", 
            "2030 (25% EV)" = "#017351",
            "2035 (54% EV)" = "#EF6A32",
            "2040 (81% EV)" = "#ED0345",
            "2045 (95% EV)" = "#710162")
b <- 0.05
plotSave <- ggplot()+
  geom_freqpoly(data=feeder.vs[(overload_cat=='EV Overload'|overload_cat=='No Overload')
                               &min_headroom_share<1&min_headroom_share>0
                               &(year==2025)],
                aes(min_headroom_share,
                    group=overload_cat,
                    color='2025 ( 8% EV)',
                    y=after_stat(count[group==1]/(count[group==1]+count[group==2]))),
                position="identity",
                breaks = seq(0,1,by=b))+
  geom_freqpoly(data=feeder.vs[(overload_cat=='EV Overload'|overload_cat=='No Overload')
                               &min_headroom_share<1&min_headroom_share>0
                               &(year==2030)],
                aes(min_headroom_share,
                    group=overload_cat,
                    color='2030 (25% EV)',
                    y=after_stat(count[group==1]/(count[group==1]+count[group==2]))),
                position="identity",
                breaks = seq(0,1,by=b))+
  geom_freqpoly(data=feeder.vs[(overload_cat=='EV Overload'|overload_cat=='No Overload')
                               &min_headroom_share<1&min_headroom_share>0
                               &(year==2035)],
                aes(min_headroom_share,
                    group=overload_cat,
                    color='2035 (54% EV)',
                    y=after_stat(count[group==1]/(count[group==1]+count[group==2]))),
                position="identity",
                breaks = seq(0,1,by=b))+
  geom_freqpoly(data=feeder.vs[(overload_cat=='EV Overload'|overload_cat=='No Overload')
                               &min_headroom_share<1&min_headroom_share>0
                               &(year==2040)],
                aes(min_headroom_share,
                    group=overload_cat,
                    color='2040 (81% EV)',
                    y=after_stat(count[group==1]/(count[group==1]+count[group==2]))),
                position="identity",
                breaks = seq(0,1,by=b))+
  geom_freqpoly(data=feeder.vs[(overload_cat=='EV Overload'|overload_cat=='No Overload')
                               &min_headroom_share<1&min_headroom_share>0
                               &(year==2045)],
                aes(min_headroom_share,
                    group=overload_cat,
                    color='2045 (95% EV)',
                    y=after_stat(count[group==1]/(count[group==1]+count[group==2]))),
                position="identity",
                breaks = seq(0,1,by=b))+
  xlab('Capacity Headroom')+
  ylab('Share of Overload Feeders')+
  labs(color = "Year")+
  scale_color_manual(values = colors)+
  scale_x_continuous(breaks = seq(0, 1, by = 0.2))+
  theme_bw()+
  theme(text = element_text(size = 15))+
  theme(legend.title = element_text(size=13),
        panel.grid.major = element_blank(),
        panel.grid.minor = element_blank())
ggsave(plotSave,file=paste0(paste0('figures/PNAS/headroom vs overload_share_years_',b,'.png')),height=4.3,width=8.6,dpi=500)

for (yr in c(2025,2030,2035,2040,2045)){
  plotSave <- ggplot()+
    geom_histogram(data=feeder.vs[(overload_cat=='EV Overload'|overload_cat=='No Overload')
                            &min_headroom_share<1&min_headroom_share>0
                            &year==yr],
                   aes(min_headroom_share,group=overload_cat,y=after_stat(count[group==1]/(count[group==1]+count[group==2]))),
                   position="dodge",
                   breaks = seq(0,1,by=0.05))+
    xlab('Capacity Headroom')+
    ylab('Share of Overload Feeders')+
    scale_x_continuous(breaks = seq(0, 1, by = 0.2))+
    ggtitle(yr)+
    theme_bw()+
    theme(text = element_text(size = 15))+
    theme(legend.title = element_text(size=13),
          panel.grid.major = element_blank(),
          panel.grid.minor = element_blank())
  ggsave(plotSave,file=paste0(paste0('figures/PNAS/test/headroom vs overload_share_',yr,'.png')),height=4,width=6,dpi=500)
}

# for (b in c(0.01,0.02,0.05,0.1)){
#   plotSave <- ggplot()+
#     geom_freqpoly(data=feeder.vs[(overload_cat=='EV Overload'|overload_cat=='No Overload')
#                                  &min_headroom_share<1&min_headroom_share>0
#                                  &(year==2025)],
#                   aes(min_headroom_share,
#                       group=overload_cat,
#                       color='2025 ( 8% EV)',
#                       y=after_stat(count[group==1]/(count[group==1]+count[group==2]))),
#                   position="identity",
#                   breaks = seq(0,1,by=b))+
#     geom_freqpoly(data=feeder.vs[(overload_cat=='EV Overload'|overload_cat=='No Overload')
#                                  &min_headroom_share<1&min_headroom_share>0
#                                  &(year==2030)],
#                   aes(min_headroom_share,
#                       group=overload_cat,
#                       color='2030 (25% EV)',
#                       y=after_stat(count[group==1]/(count[group==1]+count[group==2]))),
#                   position="identity",
#                   breaks = seq(0,1,by=b))+
#     geom_freqpoly(data=feeder.vs[(overload_cat=='EV Overload'|overload_cat=='No Overload')
#                                  &min_headroom_share<1&min_headroom_share>0
#                                  &(year==2035)],
#                   aes(min_headroom_share,
#                       group=overload_cat,
#                       color='2035 (54% EV)',
#                       y=after_stat(count[group==1]/(count[group==1]+count[group==2]))),
#                   position="identity",
#                   breaks = seq(0,1,by=b))+
#     geom_freqpoly(data=feeder.vs[(overload_cat=='EV Overload'|overload_cat=='No Overload')
#                                  &min_headroom_share<1&min_headroom_share>0
#                                  &(year==2040)],
#                   aes(min_headroom_share,
#                       group=overload_cat,
#                       color='2040 (81% EV)',
#                       y=after_stat(count[group==1]/(count[group==1]+count[group==2]))),
#                   position="identity",
#                   breaks = seq(0,1,by=b))+
#     geom_freqpoly(data=feeder.vs[(overload_cat=='EV Overload'|overload_cat=='No Overload')
#                                  &min_headroom_share<1&min_headroom_share>0
#                                  &(year==2045)],
#                   aes(min_headroom_share,
#                       group=overload_cat,
#                       color='2045 (95% EV)',
#                       y=after_stat(count[group==1]/(count[group==1]+count[group==2]))),
#                   position="identity",
#                   breaks = seq(0,1,by=b))+
#     xlab('Capacity Headroom')+
#     ylab('Share of Overload Feeders')+
#     labs(color = "Year")+
#     scale_color_manual(values = colors)+
#     scale_x_continuous(breaks = seq(0, 1, by = 0.2))+
#     theme_bw()+
#     theme(text = element_text(size = 15))+
#     theme(legend.title = element_text(size=13),
#           panel.grid.major = element_blank(),
#           panel.grid.minor = element_blank())
#   ggsave(plotSave,file=paste0(paste0('figures/PNAS/test/headroom vs overload_share_years_',b,'.png')),height=4,width=6,dpi=500)
# }



# for (yr in c(2045)){
#   plotSave <- ggplot(data=feeder.vs[(overload_cat=='EV Overload'|overload_cat=='No Overload')
#                                     &min_headroom_share<1&min_headroom_share>0
#                                     &year==yr])+
#     geom_point(aes(x=min_headroom_share,
#                    y=max_overload_intensity,
#                    color=max_type))+
#     geom_hline(yintercept = 1,color="red")+
#     geom_hline(yintercept = 0,color="red")+
#     xlab('Capacity Headroom')+
#     ylab('Total Load/Capacity')+
#     theme_bw()+
#     theme(text = element_text(size = 15))+
#     theme(legend.title = element_text(size=13),
#           panel.grid.major = element_blank(),
#           panel.grid.minor = element_blank())
#   ggsave(plotSave,file=paste0(paste0('figures/PNAS/test/headroom vs overload_scatter_',yr,'.png')),height=4,width=6,dpi=500)
# }
# 
# 

# 
# plotSave <- ggplot(data=feeder.vs[(overload_cat=='EV Overload'|overload_cat=='No Overload')
#                                   &min_headroom_share<1&min_headroom_share>0
#                                   &(year==2025|year==2030|year==2035|year==2040|year==2045)])+
#   geom_histogram(aes(min_headroom_share,
#                      fill=year,
#                      group=overload_cat,
#                      y=after_stat(count[group==1]/(count[group==1]+count[group==2]))),
#                  position="identity",
#                  breaks = seq(0,1,by=0.1))+
#   xlab('Capacity Headroom')+
#   ylab('Share of Overload Feeders')+
#   scale_x_continuous(breaks = seq(0, 1, by = 0.2))+
#   theme_bw()+
#   theme(text = element_text(size = 15))+
#   theme(legend.title = element_text(size=13),
#         panel.grid.major = element_blank(),
#         panel.grid.minor = element_blank())
# ggsave(plotSave,file=paste0(paste0('figures/PNAS/test/headroom vs overload_share_years.png')),height=4,width=6,dpi=500)
# 
# 
# 
# plotSave <- ggplot()+
#   geom_histogram(data=feeder.vs[(overload_cat=='EV Overload'|overload_cat=='No Overload')
#                                 &min_headroom_share<1&min_headroom_share>0
#                                 &(year==2045)],
#                  aes(min_headroom_share,
#                      group=overload_cat,
#                      y=after_stat(count[group==1]/(count[group==1]+count[group==2]))),
#                  position="identity",
#                  breaks = seq(0,1,by=0.1),
#                  fill=NA,
#                  color='#710162')+
#   geom_histogram(data=feeder.vs[(overload_cat=='EV Overload'|overload_cat=='No Overload')
#                                 &min_headroom_share<1&min_headroom_share>0
#                                 &(year==2040)],
#                  aes(min_headroom_share,
#                      group=overload_cat,
#                      y=after_stat(count[group==1]/(count[group==1]+count[group==2]))),
#                  position="identity",
#                  breaks = seq(0,1,by=0.1),
#                  fill=NA,
#                  color='#ED0345')+
#   geom_histogram(data=feeder.vs[(overload_cat=='EV Overload'|overload_cat=='No Overload')
#                                 &min_headroom_share<1&min_headroom_share>0
#                                 &(year==2035)],
#                  aes(min_headroom_share,
#                      group=overload_cat,
#                      y=after_stat(count[group==1]/(count[group==1]+count[group==2]))),
#                  position="identity",
#                  breaks = seq(0,1,by=0.1),
#                  fill=NA,
#                  color='#EF6A32')+
#   geom_histogram(data=feeder.vs[(overload_cat=='EV Overload'|overload_cat=='No Overload')
#                                 &min_headroom_share<1&min_headroom_share>0
#                                 &(year==2030)],
#                  aes(min_headroom_share,
#                      group=overload_cat,
#                      y=after_stat(count[group==1]/(count[group==1]+count[group==2]))),
#                  position="identity",
#                  breaks = seq(0,1,by=0.1),
#                  fill=NA,
#                  color='#017351')+
#   geom_histogram(data=feeder.vs[(overload_cat=='EV Overload'|overload_cat=='No Overload')
#                                 &min_headroom_share<1&min_headroom_share>0
#                                 &(year==2025)],
#                  aes(min_headroom_share,
#                      group=overload_cat,
#                      y=after_stat(count[group==1]/(count[group==1]+count[group==2]))),
#                  position="identity",
#                  breaks = seq(0,1,by=0.1),
#                  fill=NA,
#                  color='#26294A')+
#   xlab('Capacity Headroom')+
#   ylab('Share of Overload Feeders')+
#   scale_x_continuous(breaks = seq(0, 1, by = 0.2))+
#   theme_bw()+
#   theme(text = element_text(size = 15))+
#   theme(legend.title = element_text(size=13),
#         panel.grid.major = element_blank(),
#         panel.grid.minor = element_blank())
# ggsave(plotSave,file=paste0(paste0('figures/PNAS/test/headroom vs overload_share_years.png')),height=4,width=6,dpi=500)
# 
# 
# 
# for (yr in c(2045)){
#   plotSave <- ggplot()+
#     geom_histogram(data=feeder.vs[overload_cat=='EV Overload'&min_headroom_share<1&min_headroom_share>0&year==yr],
#                   aes(min_headroom_share,y=after_stat(count)),
#                   size=0.7,
#                   position="identity",
#                   breaks = seq(0,1,by=0.1))+
#     ggtitle(paste0("Number of Overloaded Feeder"))+
#     xlab('Capacity Headroom')+
#     ylab('Count of Overload Feeders')+
#     scale_x_continuous(breaks = seq(0, 1, by = 0.2))+
#     theme_bw()+
#     theme(text = element_text(size = 15))+
#     theme(legend.title = element_text(size=13),
#           panel.grid.major = element_blank(),
#           panel.grid.minor = element_blank())
#   ggsave(plotSave,file=paste0(paste0('figures/PNAS/test/headroom vs overload_count_',yr,'.png')),height=4,width=6,dpi=500)
# }



