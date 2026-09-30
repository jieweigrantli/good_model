library(data.table)
library(ggplot2)


rm(list=ls())
charge.all <- readRDS("data/charging data/charging_session_all_clean.rds")
head(charge.all)
unique(charge.all$charge_type)

charge.all[,charge_type_category:='none']
charge.all[housing=='multi family',charge_type_category:='home-multi family']
charge.all[housing=='single family',charge_type_category:='home-single family']
charge.all[charge_type=='work',charge_type_category:='work']
charge.all[charge_type=='public'&charger_level=='DC',charge_type_category:='public-DC']
charge.all[charge_type=='public'&charger_level=='L2',charge_type_category:='public-L2']

plotSave <- ggplot() +
  geom_freqpoly(data=charge.all, aes(start_hour,color=charge_type_category,after_stat(density)),
                #position="identity",
                binwidth = 1,
                size=0.7)+
  xlab('Charge Event Start Hour')+
  ylab('Density')+
  labs(color = "EV Charging Type")+
  scale_color_brewer(palette="Set1")+
  #xlim(0,10)+
  scale_x_continuous(breaks = seq(0, 24, by = 6))+
  #theme_minimal()+
  theme_bw()+
  #theme(legend.position="bottom")+
  theme(rect = element_rect(fill = "transparent"),
        plot.background = element_rect(fill = "transparent",colour = NA),
        text = element_text(size = 15),
        panel.grid.major = element_blank(),
        panel.grid.minor = element_blank())
ggsave(plotSave,file=paste0('figures/PNAS/event_StartHour.png'),height=4,width=6,dpi=500,bg="transparent")
ggsave(plotSave,file=paste0('figures/PNAS/TIFF/event_StartHour.tiff'),height=4,width=6,dpi=500,bg="transparent")

# plotSave <- ggplot(data=charge.all, aes(start_hour,color=charge_type_category)) +
#   geom_density(size=0.7)+
#   #geom_density()+
#   xlab('Charge Event Start Hour')+
#   ylab('Density')+
#   labs(color = "EV Charging Type")+
#   #xlim(0,10)+
#   scale_x_continuous(breaks = seq(0, 24, by = 6))+
#   #theme_minimal()+
#   theme_bw()+
#   #theme(legend.position="bottom")+
#   theme(rect = element_rect(fill = "transparent"),
#         plot.background = element_rect(fill = "transparent",colour = NA),
#         text = element_text(size = 15),
#         panel.grid.major = element_blank(),
#         panel.grid.minor = element_blank())
# ggsave(plotSave,file=paste0('figures/PNAS/event_StartHour.png'),height=4,width=6,dpi=500,bg="transparent")
# ggsave(plotSave,file=paste0('figures/PNAS/TIFF/event_StartHour.tiff'),height=4,width=6,dpi=500,bg="transparent")



