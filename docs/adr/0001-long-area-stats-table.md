# 1. One long area-stats table as the contract between build and app

Every value is stored as a row: `(level, area_id, variable, category, estimate, se, source, confidence)`. The alternative was a wide table with one column per variable category.

We chose the long table because the spec requires that a new variable appears in the app without code changes. With a long table, a new variable is just new rows, and the API, filters, legend, detail panel and export already handle it. Published comparison values (county, state) fit in the same table with `source = published`.

The cost is pivoting at read time, and more rows to scan. That's trivial for Portland, about 31K parcels. For national scale, the table is partitioned by `level` and state in Parquet, and map values for each level are pre-aggregated.
