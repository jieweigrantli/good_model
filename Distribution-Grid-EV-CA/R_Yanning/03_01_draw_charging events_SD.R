library(data.table)

EV.trips.sd <- readRDS('data/mobility data/CSTDM processed/EV trips_new_SDPTM_sample42.rds')

# generate ID for each TOUR
EV.trips.sd[, TourID := .GRP, by=.(SerialNo,Person,Tour)]
EV.trips.sd <- EV.trips.sd[,c("TourID","Trip","J","Dist","Time","year","ChargeType")]
# The prob share of charging location choices in EVMT survey - each "choice" can include more than one location
survey.share <- data.table('H'=c(1,0,0,1,1,0,1),
                           'W'=c(0,1,0,1,0,1,1),
                           'P'=c(0,0,1,0,1,1,1),
                           'Share'=c(0.53,0.08,0.03,0.16,0.13,0.03,0.04))

#=========== charging type to possible charging location choices ==================

# get the charging types that take place in each TOUR
tour.types <- EV.trips.sd[,c("TourID","ChargeType")]
tour.types <- unique(tour.types)
# long to wide
tour.types <- dcast(tour.types,TourID ~ ChargeType,value.var = 'ChargeType')
# use 0 & 1 to denote whether each charging type exist in a tour
tour.types[H=='H',Htype:=1]
tour.types[is.na(H),Htype:=0]
tour.types[W=='W',Wtype:=1]
tour.types[is.na(W),Wtype:=0]
tour.types[P=='P',Ptype:=1]
tour.types[is.na(P),Ptype:=0]
tour.types[,H:=NULL]
tour.types[,W:=NULL]
tour.types[,P:=NULL]
# insert the 7 charging location choices in each tour
tour.types <- tour.types[rep(1:.N,each=7),]
tour.types[,Hloc:=rep(c(1,0,0,1,1,0,1),times=nrow(tour.types)/7)]
tour.types[,Wloc:=rep(c(0,1,0,1,0,1,1),times=nrow(tour.types)/7)]
tour.types[,Ploc:=rep(c(0,0,1,0,1,1,1),times=nrow(tour.types)/7)]
# multiply the corresponding columns to get the location choices for each tour
tour.types[,H:=Htype*Hloc]
tour.types[,W:=Wtype*Wloc]
tour.types[,P:=Ptype*Ploc]
tour.types <- unique(tour.types[,c('TourID','H','W','P')])
# merge the share for each choice & calculate prob within each tour
tour.types <- merge(x=tour.types,y=survey.share,
                    by=c('H','W','P')) 
tour.types[,one:=sum(Share),keyby=TourID]
tour.types[,Prob:=Share/one] 
saveRDS(tour.types,file='data/mobility data/CSTDM processed/charge_choice_tour_SDPTM_sample42.rds')

head(tour.types)

#========== draw the charging location choice & which trips to charge =================
start_time <- Sys.time()
# draw the charging location choice (one for each tour)
set.seed(12)
tour.choice <- tour.types[,.SD[sample(x=.N,size=1,prob=Prob)],by=TourID]
end_time <- Sys.time()
draw_time <- end_time - start_time # around 2 hours
# filter the trips that have charging event afterwards
charge.trip <- merge(x=EV.trips.sd,y=tour.choice[,c('H','W','P','TourID')],by="TourID") 
charge.trip <- melt(charge.trip, id.vars = c('TourID','Trip','ChargeType'), 
                   measure.vars = c('H','W','P')) 
charge.trip <- charge.trip[value==1 & ChargeType==variable] # (replace 2)this table should be separate from 0 and 4/5

#=========== calculate distance/demand for each charging event ===============
# get the order of the charging events
setorder(charge.trip,TourID,Trip)
charge.trip[,chargeEvent:=1:.N, by = c("TourID")] 
saveRDS(charge.trip,file='data/mobility data/CSTDM processed/charge_trip_SDPTM_sample42_draw12.rds')
# join the trips with charging events back into all trips: rolling to the next closest charging event
charge.trip.all <- charge.trip[,c('TourID','Trip','chargeEvent')][EV.trips.sd[,c('TourID','Trip','ChargeType','Dist')],on=.(TourID,Trip),roll=-Inf] 
setorder(charge.trip.all,TourID,Trip)
saveRDS(charge.trip.all,file='data/mobility data/CSTDM processed/charge_trip_all_SDPTM_sample42_draw12.rds')

head(charge.trip.all)
head(EV.trips.sd)
summary(charge.trip.all)
summary(EV.trips.sd)
## need to check why there are 140 more obs.

# sum the trip distances by charging event
charge.event <- charge.trip.all[!is.na(chargeEvent),
                                .(chargeDist=sum(Dist)), 
                                by=.(TourID,chargeEvent)] 

# get the trip IDs & charge types back into each charging event
charge.event <- merge(x=charge.event,y=charge.trip[,c('TourID','chargeEvent','Trip','ChargeType')],
                     by=c('TourID','chargeEvent')) 

# merge the other columns needed into each charging event
charge.event <- merge(x=charge.event,y=EV.trips.sd[,c('TourID','Trip','J','Time','year')], 
                     by=c('TourID','Trip')) 
## still 140 more than "charge.trip"...TourID & trip not identical?
head(charge.event)
# note that this is still NEW charging events for each year
saveRDS(charge.event,file='data/mobility data/CSTDM processed/charge_event_new_SDPTM_sample42_draw12.rds')

nrow(unique(EV.trips.sd[,c('TourID','Trip')])) # 144700674/144700818...must have duplicate trip IDs
nrow(unique(EV.trips.sd)) # 144700790/144700818 some trips with same IDs have diff attributes while some are identical
