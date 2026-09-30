# Draft: SDG&E ICA bulk data request

**To:** sdgeinterconnectionmap@sdge.com
**Cc:** Alan Jenn <ajenn@ucdavis.edu>
**Subject:** Academic data request — bulk ICA circuit and substation data for UC Davis research

---

Dear SDG&E Interconnection Map team,

I am a PhD researcher at the Institute of Transportation Studies, UC Davis, working with
Professor Alan Jenn on a study of how electric vehicle charging demand interacts with
California's electricity delivery system. The work extends the feeder-level analysis
published by Li and Jenn in *PNAS* (2024), "Impact of electric vehicle charging demand on
power distribution grid congestion," which used SDG&E and PG&E Integration Capacity Analysis
results alongside SCE Grid Needs Assessment data.

Our current study adds a transmission-constrained layer, so we need to attach EV charging
demand to the substations that actually serve it rather than to the nearest substation
geographically. We have already integrated the equivalent public datasets from the other two
investor-owned utilities:

- PG&E, via the bulk Grid Resource Integration Portal shapefile download
- SCE, via the DRPEP ArcGIS REST services

For SDG&E we have not been able to locate a bulk download or service endpoint. The
Interconnection Map at `interconnectionmapsdge.extweb.sempra.com` loads its layers
dynamically, and the ICA User Guide does not document a data export path.

We would be grateful for access to the following, in any convenient format (shapefile,
GeoPackage, CSV, or a REST service URL):

1. **Distribution circuit geometry**, with each circuit's parent substation name or ID.
2. **Substation locations**, with name, ID, and operating voltage.
3. **ICA results by line section or node** — load capacity and, if available, the hourly or
   month-hour profile.
4. **Circuit and substation load profiles** — the historical or forecast demand series
   underlying the ICA.
5. **Substation or transformer bank ratings**, if published separately from ICA.

To be clear about our intent: this is non-commercial academic research, the results will be
published in the peer-reviewed literature and in a doctoral dissertation, and SDG&E will be
cited as the data source. We are happy to work within any terms of use, to sign a data use
agreement, or to restrict the resolution at which results are reported if that helps address
any confidentiality or infrastructure-security concerns. We are equally happy to share our
findings for SDG&E's territory with your team ahead of publication.

If a bulk export is not available, we would also welcome guidance on the correct route —
whether that is a formal data request process, a CPUC proceeding docket, or a contact in your
distribution planning group.

Thank you for your time, and for maintaining the public ICA resources.

Best regards,

Jiewei (Grant) Li
PhD Researcher, Institute of Transportation Studies
University of California, Davis
jwgli@ucdavis.edu

---

## Notes before sending

- **Ask Alan first.** He is a co-author of Li & Jenn (2024), and that paper used SDG&E ICA
  data with hourly resolution. Whatever route they took to obtain it is a faster and more
  certain path than a cold request, and he may simply still have the files. This email is the
  fallback if that route has gone stale.
- Confirm Alan Jenn's preferred email address and whether he wants to be cc'd or to send it
  himself; a request from a faculty PI often moves faster.
- If there is an existing CPUC docket contact for the Distribution Resources Plan
  (R.14-08-013 and successors), copying CPUC Energy Division staff may help, since the data
  portals exist under a CPUC mandate (D.24-10-030 most recently).
- Item 5 is the one most likely to be refused on security grounds. Items 1–2 are already
  visible in the public map, so those should be uncontroversial.
