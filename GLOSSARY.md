# Maine Synthetic Population

A parcel-level synthetic population for Portland, Maine, built from public Census data. It is a general base layer that other attributes (for example diabetes or obesity estimates) can be attached to later.

## Language

### Population

**Synthetic population**:
The full set of synthetic households and synthetic persons placed on parcels in the study area. It is an estimate of the population's distribution, not a record of real residents.
_Avoid_: Simulated population, fake data, model output

**Synthetic household**:
A copy of one seed household, assigned to one housing unit on one parcel.
_Avoid_: Simulated household, agent

**Synthetic person**:
A member of a synthetic household (or a group-quarters resident), carrying the attributes of the seed person it was copied from.
_Avoid_: Agent, individual, resident

**Seed**:
The anonymised Census microdata (PUMS) records for the PUMA that contains the study area. Every synthetic household and person is a copy of a seed record.
_Avoid_: Sample, training data

**Group-quarters resident**:
A person living in an institution or shared facility (nursing home, dorm, jail, shelter) rather than a housing unit.
_Avoid_: GQ person, institutional population

### Geography

**Study area**:
The City of Portland, Maine. Only places inside it are modelled.
_Avoid_: Portland metro, Greater Portland

**Parcel**:
A piece of land with an ID and boundary from the Maine statewide parcel layer. It is the smallest level at which synthetic households are placed.
_Avoid_: Lot, property, plot

**Comparison level**:
A geography shown beside a selected area for context (block group, county, state). County and state use published estimates because they extend beyond the study area.

**Level**:
One rung of the geography hierarchy: parcel, block, block group, tract, city, county, state. Every area belongs to exactly one area at each level above it.
_Avoid_: Resolution, scale, tier

**Placement**:
The assignment of one synthetic household to one housing unit on one parcel.
_Avoid_: Assignment, allocation

### Runs

**Run**:
One complete build of the synthetic population.

**Main run**:
The run that uses the main PUMS weights. Its totals are the estimates the app shows.

**Replicate run**:
One of 80 runs that each use one set of PUMS replicate weights. The spread across replicate runs gives the standard error.

**Validation run**:
A main run with 20% of blocks held out. It is used only for the validation report, never shown in the app.

### Estimates

**Synthetic estimate**:
A value calculated by adding up the synthetic population. Every value inside the study area is a synthetic estimate.
_Avoid_: Prediction, model result

**Published estimate**:
A value taken directly from a Census table (ACS or the 2020 Census), with its published margin of error.
_Avoid_: Ground truth, actual value

**Count confidence**:
How sure we are of the number of households and persons on a parcel or area.

**Composition confidence**:
How sure we are of who those households and persons are (age, sex, race/ethnicity, education). Below the block level it is always lower than count confidence.
