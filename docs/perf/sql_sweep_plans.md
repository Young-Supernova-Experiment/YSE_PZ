# SQL sweep measurements (synthetic MySQL 8.0.25, 35k transients / 800k photometry rows, 128 MB buffer pool)

before = PK + FK indexes only (production today); after = the eight indexes in migration 0010. Wall time = best of 2 warm runs of the statement itself.

| query | before old ms | before new ms | after old ms | after new ms | rows | identical row set | identical order |
|---|---|---|---|---|---|---|---|
| saved 62 YSE Magnitude-Limited Sample (min mag < 18.6) | 3559 | 730 | 4549 | 2403 | 27998 | True | True |
| saved 254 Fast & Young Target Search | 1656 | 417 | 2606 | 349 | 255 | True | True |
| saved 217 New Transients Last Two Days | 1600 | 411 | 2579 | 300 | 451 | True | True |
| saved 11 Interesting New Targets | 3201 | 2746 | 4147 | 3763 | 799 | True | True |
| code recent_mag annotation (table_utils/yse_views/views), 2000 rows | 466 | 309 | 183 | 135 | 2000 | True | True |
| code days_since_disc annotation (yse_views), 2000 rows | 18 | 15 | 22 | 18 | 2000 | True | True |
| code rising-transient last_mag_query x10 annotations (yse_python_queries), 400 rows | 442 | 141 | 466 | 208 | 400 | True | True |

## saved 62 YSE Magnitude-Limited Sample (min mag < 18.6)

### before: old plan (3559 ms)
```
-> Nested loop inner join  (cost=15500.63 rows=507) (actual time=1.013..5057.739 rows=27998 loops=1)
    -> Nested loop inner join  (cost=14306.31 rows=507) (actual time=1.007..5001.716 rows=27998 loops=1)
        -> Nested loop inner join  (cost=9021.25 rows=1522) (actual time=0.506..102.319 rows=28000 loops=1)
            -> Inner hash join (no condition)  (cost=3620.58 rows=1522) (actual time=0.363..40.600 rows=35000 loops=1)
                -> Filter: ((t.`name` like '202%') or (t.`name` like '201%'))  (cost=3619.48 rows=7252) (actual time=0.035..31.671 rows=35000 loops=1)
                    -> Table scan on t  (cost=3619.48 rows=34559) (actual time=0.033..19.890 rows=35000 loops=1)
                -> Hash
                    -> Filter: (tg.`name` = 'YSE')  (cost=1.10 rows=1) (actual time=0.313..0.315 rows=1 loops=1)
                        -> Table scan on tg  (cost=1.10 rows=1) (actual time=0.307..0.309 rows=1 loops=1)
            -> Single-row index lookup on tt using YSE_App_transient_tags_transient_id_transientta_072a9823_uniq (transient_id=t.id, transienttag_id=tg.id)  (cost=0.72 rows=1) (actual time=0.002..0.002 rows=1 loops=35000)
        -> Filter: ((pd.mag < 18.6) and (pd.id = (select #2)))  (cost=0.71 rows=0) (actual time=0.175..0.175 rows=1 loops=28000)
            -> Single-row index lookup on pd using PRIMARY (id=(select #2))  (cost=0.71 rows=1) (actual time=0.129..0.129 rows=1 loops=28000)
            -> Select #2 (subquery in condition; dependent)
                -> Limit: 1 row(s)  (actual time=0.057..0.057 rows=1 loops=83998)
                    -> Sort: pd2.mag, limit input to 1 row(s) per chunk  (actual time=0.057..0.057 rows=1 loops=83998)
                        -> Stream results  (cost=23.38 rows=23) (actual time=0.033..0.054 rows=22 loops=83998)
                            -> Nested loop antijoin  (cost=23.38 rows=23) (actual time=0.033..0.052 rows=22 loops=83998)
                                -> Nested loop inner join  (cost=18.84 rows=23) (actual time=0.031..0.042 rows=22 loops=83998)
                                    -> Index lookup on p2 using YSE_App_transientpho_transient_id_9770bdf2_fk_YSE_App_t (transient_id=t.id)  (cost=0.50 rows=1) (actual time=0.001..0.002 rows=1 loops=83998)
                                    -> Filter: (((pd2.mag is null) = false) and ((pd2.flux / pd2.flux_err) > 3))  (cost=15.74 rows=20) (actual time=0.029..0.034 rows=19 loops=95998)
                                        -> Index lookup on pd2 using YSE_App_transientpho_photometry_id_fbc5dfd2_fk_YSE_App_t (photometry_id=p2.id)  (cost=15.74 rows=20) (actual time=0.029..0.032 rows=20 loops=95998)
                                -> Single-row index lookup on <subquery3> using <auto_distinct_key> (transientphotdata_id=pd2.id)  (actual time=0.000..0.000 rows=0 loops=1848169)
                                    -> Materialize with deduplication  (cost=1.20..1.20 rows=1) (actual time=0.000..0.000 rows=0 loops=1848169)
                                        -> Filter: (dq.transientphotdata_id is not null)  (cost=1.10 rows=1) (actual time=0.001..0.001 rows=0 loops=83998)
                                            -> Index scan on dq using YSE_App_transientphotdat_transientphotdata_id_dat_71c1d877_uniq  (cost=1.10 rows=1) (actual time=0.001..0.001 rows=0 loops=83998)
    -> Single-row index lookup on p using PRIMARY (id=pd.photometry_id)  (cost=0.47 rows=1) (actual time=0.002..0.002 rows=1 loops=27998)

```
### before: new plan (730 ms)
```
-> Nested loop inner join  (cost=49893.31 rows=0) (actual time=1021.268..1120.872 rows=27998 loops=1)
    -> Nested loop inner join  (cost=12525.95 rows=5807) (actual time=0.138..72.026 rows=28000 loops=1)
        -> Nested loop inner join  (cost=2840.40 rows=27673) (actual time=0.125..29.369 rows=28000 loops=1)
            -> Filter: (tg.`name` = 'YSE')  (cost=0.35 rows=1) (actual time=0.026..0.071 rows=1 loops=1)
                -> Table scan on tg  (cost=0.35 rows=1) (actual time=0.022..0.066 rows=1 loops=1)
            -> Index lookup on tt using YSE_App_transient_ta_transienttag_id_26eb4c68_fk_YSE_App_t (transienttag_id=tg.id)  (cost=2840.05 rows=27673) (actual time=0.098..27.516 rows=28000 loops=1)
        -> Filter: ((t.`name` like '202%') or (t.`name` like '201%'))  (cost=0.25 rows=0) (actual time=0.001..0.001 rows=1 loops=28000)
            -> Single-row index lookup on t using PRIMARY (id=tt.transient_id)  (cost=0.25 rows=1) (actual time=0.001..0.001 rows=1 loops=28000)
    -> Index lookup on g using <auto_key0> (transient_id=tt.transient_id)  (actual time=0.000..0.001 rows=1 loops=28000)
        -> Materialize  (cost=0.00..0.00 rows=0) (actual time=0.037..0.037 rows=1 loops=28000)
            -> Filter: (min(pd2.mag) < 18.6)  (actual time=992.046..998.732 rows=34998 loops=1)
                -> Table scan on <temporary>  (actual time=0.002..1.550 rows=35000 loops=1)
                    -> Aggregate using temporary table  (actual time=992.039..995.752 rows=35000 loops=1)
                        -> Nested loop inner join  (cost=619512.08 rows=712246) (actual time=0.092..830.515 rows=770116 loops=1)
                            -> Nested loop antijoin  (cost=228227.80 rows=712246) (actual time=0.084..601.344 rows=770116 loops=1)
                                -> Filter: ((pd2.mag is not null) and ((pd2.flux / pd2.flux_err) > 3))  (cost=85778.68 rows=712246) (actual time=0.073..325.728 rows=770116 loops=1)
                                    -> Table scan on pd2  (cost=85778.68 rows=791384) (actual time=0.069..252.168 rows=800000 loops=1)
                                -> Single-row index lookup on <subquery3> using <auto_distinct_key> (transientphotdata_id=pd2.id)  (actual time=0.000..0.000 rows=0 loops=770116)
                                    -> Materialize with deduplication  (cost=1.20..1.20 rows=1) (actual time=0.000..0.000 rows=0 loops=770116)
                                        -> Filter: (dq.transientphotdata_id is not null)  (cost=1.10 rows=1) (actual time=0.006..0.006 rows=0 loops=1)
                                            -> Index scan on dq using YSE_App_transientphotdat_transientphotdata_id_dat_71c1d877_uniq  (cost=1.10 rows=1) (actual time=0.006..0.006 rows=0 loops=1)
                            -> Single-row index lookup on p2 using PRIMARY (id=pd2.photometry_id)  (cost=0.45 rows=1) (actual time=0.000..0.000 rows=1 loops=770116)

```

### after: old plan (4549 ms)
```
-> Nested loop inner join  (cost=24105.68 rows=4612) (actual time=1.083..6374.841 rows=27998 loops=1)
    -> Nested loop inner join  (cost=21243.63 rows=4612) (actual time=1.078..6296.375 rows=27998 loops=1)
        -> Nested loop inner join  (cost=12648.48 rows=13837) (actual time=0.481..147.444 rows=28000 loops=1)
            -> Nested loop inner join  (cost=2962.93 rows=27673) (actual time=0.467..64.457 rows=28000 loops=1)
                -> Filter: (tg.`name` = 'YSE')  (cost=0.35 rows=1) (actual time=0.020..0.469 rows=1 loops=1)
                    -> Table scan on tg  (cost=0.35 rows=1) (actual time=0.016..0.465 rows=1 loops=1)
                -> Index lookup on tt using YSE_App_transient_ta_transienttag_id_26eb4c68_fk_YSE_App_t (transienttag_id=tg.id)  (cost=2962.58 rows=27673) (actual time=0.446..61.541 rows=28000 loops=1)
            -> Filter: ((t.`name` like '202%') or (t.`name` like '201%'))  (cost=0.25 rows=1) (actual time=0.003..0.003 rows=1 loops=28000)
                -> Single-row index lookup on t using PRIMARY (id=tt.transient_id)  (cost=0.25 rows=1) (actual time=0.002..0.002 rows=1 loops=28000)
        -> Filter: ((pd.mag < 18.6) and (pd.id = (select #2)))  (cost=0.52 rows=0) (actual time=0.219..0.219 rows=1 loops=28000)
            -> Single-row index lookup on pd using PRIMARY (id=(select #2))  (cost=0.52 rows=1) (actual time=0.173..0.173 rows=1 loops=28000)
            -> Select #2 (subquery in condition; dependent)
                -> Limit: 1 row(s)  (actual time=0.072..0.072 rows=1 loops=83998)
                    -> Sort: pd2.mag, limit input to 1 row(s) per chunk  (actual time=0.071..0.071 rows=1 loops=83998)
                        -> Stream results  (cost=19.14 rows=23) (actual time=0.047..0.069 rows=22 loops=83998)
                            -> Nested loop antijoin  (cost=19.14 rows=23) (actual time=0.047..0.067 rows=22 loops=83998)
                                -> Nested loop inner join  (cost=14.60 rows=23) (actual time=0.045..0.056 rows=22 loops=83998)
                                    -> Index lookup on p2 using YSE_App_transientpho_transient_id_9770bdf2_fk_YSE_App_t (transient_id=t.id)  (cost=0.50 rows=1) (actual time=0.002..0.002 rows=1 loops=83998)
                                    -> Filter: (((pd2.mag is null) = false) and ((pd2.flux / pd2.flux_err) > 3))  (cost=12.05 rows=20) (actual time=0.041..0.046 rows=19 loops=95998)
                                        -> Index lookup on pd2 using YSE_App_transientpho_photometry_id_fbc5dfd2_fk_YSE_App_t (photometry_id=p2.id)  (cost=12.05 rows=20) (actual time=0.041..0.044 rows=20 loops=95998)
                                -> Single-row index lookup on <subquery3> using <auto_distinct_key> (transientphotdata_id=pd2.id)  (actual time=0.000..0.000 rows=0 loops=1848169)
                                    -> Materialize with deduplication  (cost=1.20..1.20 rows=1) (actual time=0.000..0.000 rows=0 loops=1848169)
                                        -> Filter: (dq.transientphotdata_id is not null)  (cost=1.10 rows=1) (actual time=0.001..0.001 rows=0 loops=83998)
                                            -> Index scan on dq using YSE_App_transientphotdat_transientphotdata_id_dat_71c1d877_uniq  (cost=1.10 rows=1) (actual time=0.001..0.001 rows=0 loops=83998)
    -> Single-row index lookup on p using PRIMARY (id=pd.photometry_id)  (cost=0.52 rows=1) (actual time=0.003..0.003 rows=1 loops=27998)

```
### after: new plan (2403 ms)
```
-> Nested loop inner join  (cost=101871.27 rows=0) (actual time=3146.024..3296.036 rows=27998 loops=1)
    -> Nested loop inner join  (cost=12525.95 rows=13837) (actual time=0.406..119.239 rows=28000 loops=1)
        -> Nested loop inner join  (cost=2840.40 rows=27673) (actual time=0.395..65.146 rows=28000 loops=1)
            -> Filter: (tg.`name` = 'YSE')  (cost=0.35 rows=1) (actual time=0.027..0.820 rows=1 loops=1)
                -> Table scan on tg  (cost=0.35 rows=1) (actual time=0.024..0.816 rows=1 loops=1)
            -> Index lookup on tt using YSE_App_transient_ta_transienttag_id_26eb4c68_fk_YSE_App_t (transienttag_id=tg.id)  (cost=2840.05 rows=27673) (actual time=0.367..62.578 rows=28000 loops=1)
        -> Filter: ((t.`name` like '202%') or (t.`name` like '201%'))  (cost=0.25 rows=1) (actual time=0.002..0.002 rows=1 loops=28000)
            -> Single-row index lookup on t using PRIMARY (id=tt.transient_id)  (cost=0.25 rows=1) (actual time=0.001..0.002 rows=1 loops=28000)
    -> Index lookup on g using <auto_key0> (transient_id=tt.transient_id)  (actual time=0.001..0.001 rows=1 loops=28000)
        -> Materialize  (cost=0.00..0.00 rows=0) (actual time=0.113..0.113 rows=1 loops=28000)
            -> Filter: (min(pd2.mag) < 18.6)  (actual time=0.532..3099.397 rows=34998 loops=1)
                -> Group aggregate: min(pd2.mag), min(pd2.mag)  (actual time=0.527..3093.045 rows=35000 loops=1)
                    -> Nested loop antijoin  (cost=639388.42 rows=714721) (actual time=0.457..2994.303 rows=770116 loops=1)
                        -> Nested loop inner join  (cost=496444.20 rows=714721) (actual time=0.445..2660.396 rows=770116 loops=1)
                            -> Index scan on p2 using YSE_App_transientpho_transient_id_9770bdf2_fk_YSE_App_t  (cost=4130.64 rows=40132) (actual time=0.024..15.491 rows=40000 loops=1)
                            -> Filter: ((pd2.mag is not null) and ((pd2.flux / pd2.flux_err) > 3))  (cost=10.29 rows=18) (actual time=0.060..0.065 rows=19 loops=40000)
                                -> Index lookup on pd2 using YSE_App_transientpho_photometry_id_fbc5dfd2_fk_YSE_App_t (photometry_id=p2.id)  (cost=10.29 rows=20) (actual time=0.059..0.062 rows=20 loops=40000)
                        -> Single-row index lookup on <subquery3> using <auto_distinct_key> (transientphotdata_id=pd2.id)  (actual time=0.000..0.000 rows=0 loops=770116)
                            -> Materialize with deduplication  (cost=1.20..1.20 rows=1) (actual time=0.000..0.000 rows=0 loops=770116)
                                -> Filter: (dq.transientphotdata_id is not null)  (cost=1.10 rows=1) (actual time=0.007..0.007 rows=0 loops=1)
                                    -> Index scan on dq using YSE_App_transientphotdat_transientphotdata_id_dat_71c1d877_uniq  (cost=1.10 rows=1) (actual time=0.007..0.007 rows=0 loops=1)

```

## saved 254 Fast & Young Target Search

### before: old plan (1656 ms)
```
-> Sort: g.first_detection DESC  (actual time=1996.377..1996.391 rows=255 loops=1)
    -> Table scan on <temporary>  (cost=2.50..2.50 rows=0) (actual time=0.002..0.022 rows=255 loops=1)
        -> Temporary table with deduplication  (cost=23233.29..23233.29 rows=0) (actual time=1996.259..1996.294 rows=255 loops=1)
            -> Nested loop inner join  (cost=23230.79 rows=0) (actual time=1956.674..1995.950 rows=255 loops=1)
                -> Inner hash join (t.obs_group_id = og.id)  (cost=3612.51 rows=346) (actual time=0.226..18.266 rows=35000 loops=1)
                    -> Filter: (t.TNS_spec_class is null)  (cost=3611.41 rows=3456) (actual time=0.015..14.217 rows=35000 loops=1)
                        -> Table scan on t  (cost=3611.41 rows=34559) (actual time=0.014..11.653 rows=35000 loops=1)
                    -> Hash
                        -> Table scan on og  (cost=1.10 rows=1) (actual time=0.198..0.200 rows=1 loops=1)
                -> Index lookup on g using <auto_key0> (id=t.id)  (actual time=0.000..0.000 rows=0 loops=35000)
                    -> Materialize  (cost=0.00..0.00 rows=0) (actual time=0.056..0.056 rows=0 loops=35000)
                        -> Filter: (((<cache>(to_days(curdate())) - to_days(min(pd.obs_date))) < 8) and ((<cache>(to_days(curdate())) - to_days(max(pd.obs_date))) < 3) and ((to_days(max(pd.obs_date)) - to_days(min(pd.obs_date))) > 0.01) and (count(pd.obs_date) > 2))  (actual time=2.646..1954.051 rows=255 loops=1)
                            -> Group aggregate: min(pd.obs_date), max(pd.obs_date), count(pd.obs_date)  (actual time=0.394..1943.738 rows=35000 loops=1)
                                -> Nested loop inner join  (cost=640514.19 rows=784731) (actual time=0.338..1837.810 rows=794444 loops=1)
                                    -> Nested loop inner join  (cost=20748.06 rows=39624) (actual time=0.038..93.778 rows=40000 loops=1)
                                        -> Index scan on t using PRIMARY  (cost=3608.15 rows=34559) (actual time=0.028..11.702 rows=35000 loops=1)
                                        -> Index lookup on tp using YSE_App_transientpho_transient_id_9770bdf2_fk_YSE_App_t (transient_id=t.id)  (cost=0.38 rows=1) (actual time=0.002..0.002 rows=1 loops=35000)
                                    -> Filter: (((pd.flux / pd.flux_err) > 2) or (pd.mag_err < 0.2) or ((pd.mag_err is null) and (pd.mag is not null)))  (cost=13.66 rows=20) (actual time=0.038..0.042 rows=20 loops=40000)
                                        -> Index lookup on pd using YSE_App_transientpho_photometry_id_fbc5dfd2_fk_YSE_App_t (photometry_id=tp.id)  (cost=13.66 rows=20) (actual time=0.038..0.040 rows=20 loops=40000)

```
### before: new plan (417 ms)
```
-> Sort: g.first_detection DESC  (actual time=434.290..434.301 rows=255 loops=1)
    -> Table scan on <temporary>  (cost=2.50..2.50 rows=0) (actual time=0.001..0.013 rows=255 loops=1)
        -> Temporary table with deduplication  (cost=11.88..11.88 rows=0) (actual time=434.207..434.231 rows=255 loops=1)
            -> Nested loop inner join  (cost=9.38 rows=0) (actual time=433.436..434.074 rows=255 loops=1)
                -> Inner hash join (no condition)  (cost=3.88 rows=0) (actual time=433.417..433.459 rows=255 loops=1)
                    -> Table scan on g  (cost=2.50..2.50 rows=0) (actual time=0.000..0.010 rows=255 loops=1)
                        -> Materialize  (cost=2.50..2.50 rows=0) (actual time=433.187..433.208 rows=255 loops=1)
                            -> Filter: (((<cache>(to_days(curdate())) - to_days(min(pd.obs_date))) < 8) and ((<cache>(to_days(curdate())) - to_days(max(pd.obs_date))) < 3) and ((to_days(max(pd.obs_date)) - to_days(min(pd.obs_date))) > 0.01) and (count(pd.obs_date) > 2))  (actual time=432.893..433.145 rows=255 loops=1)
                                -> Table scan on <temporary>  (actual time=0.002..0.053 rows=1656 loops=1)
                                    -> Aggregate using temporary table  (actual time=432.859..432.986 rows=1656 loops=1)
                                        -> Nested loop inner join  (cost=906150.79 rows=5989386) (actual time=326.313..424.916 rows=37478 loops=1)
                                            -> Nested loop inner join  (cost=307196.01 rows=302427) (actual time=326.214..333.671 rows=1888 loops=1)
                                                -> Nested loop inner join  (cost=276953.06 rows=263768) (actual time=326.198..330.454 rows=1656 loops=1)
                                                    -> Table scan on <subquery3>  (cost=0.01..3299.60 rows=263768) (actual time=0.002..0.130 rows=1656 loops=1)
                                                        -> Materialize with deduplication  (cost=247276.40..250575.98 rows=263768) (actual time=326.135..326.373 rows=1656 loops=1)
                                                            -> Nested loop inner join  (cost=220899.56 rows=263768) (actual time=0.322..322.714 rows=20181 loops=1)
                                                                -> Filter: (pd2.obs_date >= <cache>((curdate() - interval 2 day)))  (cost=33423.74 rows=263768) (actual time=0.309..312.528 rows=20181 loops=1)
                                                                    -> Table scan on pd2  (cost=33423.74 rows=791384) (actual time=0.118..274.370 rows=800000 loops=1)
                                                                -> Single-row index lookup on tp2 using PRIMARY (id=pd2.photometry_id)  (cost=0.61 rows=1) (actual time=0.000..0.000 rows=1 loops=20181)
                                                    -> Single-row index lookup on t using PRIMARY (id=`<subquery3>`.transient_id)  (cost=0.35 rows=1) (actual time=0.002..0.002 rows=1 loops=1656)
                                                -> Index lookup on tp using YSE_App_transientpho_transient_id_9770bdf2_fk_YSE_App_t (transient_id=`<subquery3>`.transient_id)  (cost=0.36 rows=1) (actual time=0.001..0.002 rows=1 loops=1656)
                                            -> Filter: (((pd.flux / pd.flux_err) > 2) or (pd.mag_err < 0.2) or ((pd.mag_err is null) and (pd.mag is not null)))  (cost=15.82 rows=20) (actual time=0.044..0.047 rows=20 loops=1888)
                                                -> Index lookup on pd using YSE_App_transientpho_photometry_id_fbc5dfd2_fk_YSE_App_t (photometry_id=tp.id)  (cost=15.82 rows=20) (actual time=0.044..0.045 rows=20 loops=1888)
                    -> Hash
                        -> Table scan on og  (cost=1.10 rows=1) (actual time=0.212..0.214 rows=1 loops=1)
                -> Filter: ((t.obs_group_id = og.id) and (t.TNS_spec_class is null))  (cost=0.25 rows=0) (actual time=0.002..0.002 rows=1 loops=255)
                    -> Single-row index lookup on t using PRIMARY (id=g.id)  (cost=0.25 rows=1) (actual time=0.002..0.002 rows=1 loops=255)

```

### after: old plan (2606 ms)
```
-> Sort: g.first_detection DESC  (actual time=2612.086..2612.099 rows=255 loops=1)
    -> Table scan on <temporary>  (cost=2.50..2.50 rows=0) (actual time=0.002..0.021 rows=255 loops=1)
        -> Temporary table with deduplication  (cost=23120.32..23120.32 rows=0) (actual time=2611.984..2612.017 rows=255 loops=1)
            -> Nested loop inner join  (cost=23117.82 rows=0) (actual time=2579.789..2611.721 rows=255 loops=1)
                -> Inner hash join (t.obs_group_id = og.id)  (cost=3598.00 rows=344) (actual time=0.780..17.044 rows=35000 loops=1)
                    -> Filter: (t.TNS_spec_class is null)  (cost=3596.90 rows=3441) (actual time=0.020..12.735 rows=35000 loops=1)
                        -> Table scan on t  (cost=3596.90 rows=34414) (actual time=0.019..10.565 rows=35000 loops=1)
                    -> Hash
                        -> Table scan on og  (cost=1.10 rows=1) (actual time=0.743..0.746 rows=1 loops=1)
                -> Index lookup on g using <auto_key0> (id=t.id)  (actual time=0.000..0.000 rows=0 loops=35000)
                    -> Materialize  (cost=0.00..0.00 rows=0) (actual time=0.074..0.074 rows=0 loops=35000)
                        -> Filter: (((<cache>(to_days(curdate())) - to_days(min(pd.obs_date))) < 8) and ((<cache>(to_days(curdate())) - to_days(max(pd.obs_date))) < 3) and ((to_days(max(pd.obs_date)) - to_days(min(pd.obs_date))) > 0.01) and (count(pd.obs_date) > 2))  (actual time=3.297..2576.422 rows=255 loops=1)
                            -> Group aggregate: min(pd.obs_date), max(pd.obs_date), count(pd.obs_date)  (actual time=0.503..2564.928 rows=35000 loops=1)
                                -> Nested loop inner join  (cost=502322.13 rows=780794) (actual time=0.443..2458.061 rows=794444 loops=1)
                                    -> Nested loop inner join  (cost=20661.65 rows=39458) (actual time=0.031..100.683 rows=40000 loops=1)
                                        -> Index scan on t using PRIMARY  (cost=3593.65 rows=34414) (actual time=0.018..18.731 rows=35000 loops=1)
                                        -> Index lookup on tp using YSE_App_transientpho_transient_id_9770bdf2_fk_YSE_App_t (transient_id=t.id)  (cost=0.38 rows=1) (actual time=0.002..0.002 rows=1 loops=35000)
                                    -> Filter: (((pd.flux / pd.flux_err) > 2) or (pd.mag_err < 0.2) or ((pd.mag_err is null) and (pd.mag is not null)))  (cost=10.23 rows=20) (actual time=0.053..0.058 rows=20 loops=40000)
                                        -> Index lookup on pd using YSE_App_transientpho_photometry_id_fbc5dfd2_fk_YSE_App_t (photometry_id=tp.id)  (cost=10.23 rows=20) (actual time=0.053..0.055 rows=20 loops=40000)

```
### after: new plan (349 ms)
```
-> Sort: g.first_detection DESC  (actual time=476.924..476.937 rows=255 loops=1)
    -> Table scan on <temporary>  (cost=2.50..2.50 rows=0) (actual time=0.001..0.014 rows=255 loops=1)
        -> Temporary table with deduplication  (cost=11.88..11.88 rows=0) (actual time=476.808..476.835 rows=255 loops=1)
            -> Nested loop inner join  (cost=9.38 rows=0) (actual time=476.094..476.687 rows=255 loops=1)
                -> Inner hash join (no condition)  (cost=3.88 rows=0) (actual time=476.075..476.122 rows=255 loops=1)
                    -> Table scan on g  (cost=2.50..2.50 rows=0) (actual time=0.000..0.011 rows=255 loops=1)
                        -> Materialize  (cost=2.50..2.50 rows=0) (actual time=475.865..475.889 rows=255 loops=1)
                            -> Filter: (((<cache>(to_days(curdate())) - to_days(min(pd.obs_date))) < 8) and ((<cache>(to_days(curdate())) - to_days(max(pd.obs_date))) < 3) and ((to_days(max(pd.obs_date)) - to_days(min(pd.obs_date))) > 0.01) and (count(pd.obs_date) > 2))  (actual time=475.514..475.815 rows=255 loops=1)
                                -> Table scan on <temporary>  (actual time=0.002..0.059 rows=1656 loops=1)
                                    -> Aggregate using temporary table  (actual time=475.481..475.627 rows=1656 loops=1)
                                        -> Nested loop inner join  (cost=229336.33 rows=867759) (actual time=0.534..465.217 rows=37478 loops=1)
                                            -> Nested loop inner join  (cost=142546.31 rows=43853) (actual time=0.305..268.045 rows=1888 loops=1)
                                                -> Nested loop inner join  (cost=138160.80 rows=38247) (actual time=0.300..263.825 rows=1656 loops=1)
                                                    -> Nested loop semijoin with duplicate removal on YSE_App_transientpho_transient_id_9770bdf2_fk_YSE_App_t  (cost=124774.33 rows=38247) (actual time=0.291..258.821 rows=1656 loops=1)
                                                        -> Index scan on tp2 using YSE_App_transientpho_transient_id_9770bdf2_fk_YSE_App_t  (cost=3510.24 rows=40132) (actual time=0.020..7.090 rows=40000 loops=1)
                                                        -> Filter: (pd2.obs_date >= <cache>((curdate() - interval 2 day)))  (cost=1.04 rows=1) (actual time=0.006..0.006 rows=0 loops=39768)
                                                            -> Index lookup on pd2 using yse_photdata_phot_obs_idx (photometry_id=tp2.id)  (cost=1.04 rows=20) (actual time=0.003..0.005 rows=20 loops=39768)
                                                    -> Single-row index lookup on t using PRIMARY (id=tp2.transient_id)  (cost=9561.86 rows=1) (actual time=0.003..0.003 rows=1 loops=1656)
                                                -> Index lookup on tp using YSE_App_transientpho_transient_id_9770bdf2_fk_YSE_App_t (transient_id=tp2.transient_id)  (cost=0.36 rows=1) (actual time=0.002..0.002 rows=1 loops=1656)
                                            -> Filter: (((pd.flux / pd.flux_err) > 2) or (pd.mag_err < 0.2) or ((pd.mag_err is null) and (pd.mag is not null)))  (cost=14.06 rows=20) (actual time=0.099..0.103 rows=20 loops=1888)
                                                -> Index lookup on pd using YSE_App_transientpho_photometry_id_fbc5dfd2_fk_YSE_App_t (photometry_id=tp.id)  (cost=14.06 rows=20) (actual time=0.098..0.100 rows=20 loops=1888)
                    -> Hash
                        -> Table scan on og  (cost=1.10 rows=1) (actual time=0.191..0.194 rows=1 loops=1)
                -> Filter: ((t.obs_group_id = og.id) and (t.TNS_spec_class is null))  (cost=0.25 rows=0) (actual time=0.002..0.002 rows=1 loops=255)
                    -> Single-row index lookup on t using PRIMARY (id=g.id)  (cost=0.25 rows=1) (actual time=0.002..0.002 rows=1 loops=255)

```

## saved 217 New Transients Last Two Days

### before: old plan (1600 ms)
```
-> Sort: g.first_detection DESC  (actual time=2221.128..2221.157 rows=451 loops=1)
    -> Table scan on <temporary>  (cost=2.50..2.50 rows=0) (actual time=0.002..0.035 rows=451 loops=1)
        -> Temporary table with deduplication  (cost=182138.52..182138.52 rows=0) (actual time=2220.959..2221.017 rows=451 loops=1)
            -> Nested loop inner join  (cost=182136.02 rows=0) (actual time=2181.501..2220.289 rows=451 loops=1)
                -> Inner hash join (t.obs_group_id = og.id)  (cost=3609.71 rows=28618) (actual time=0.463..20.623 rows=35000 loops=1)
                    -> Filter: ((t.TNS_spec_class <> 'SN Ia') or (t.TNS_spec_class is null))  (cost=3608.61 rows=31449) (actual time=0.025..15.984 rows=35000 loops=1)
                        -> Table scan on t  (cost=3608.61 rows=34559) (actual time=0.023..12.435 rows=35000 loops=1)
                    -> Hash
                        -> Table scan on og  (cost=1.10 rows=1) (actual time=0.383..0.387 rows=1 loops=1)
                -> Index lookup on g using <auto_key0> (id=t.id)  (actual time=0.000..0.000 rows=0 loops=35000)
                    -> Materialize  (cost=0.00..0.00 rows=0) (actual time=0.063..0.063 rows=0 loops=35000)
                        -> Filter: (((<cache>(to_days(curdate())) - to_days(min(pd.obs_date))) < 15) and ((<cache>(to_days(curdate())) - to_days(max(pd.obs_date))) < 2) and (count(pd.obs_date) > 2))  (actual time=3.228..2176.573 rows=451 loops=1)
                            -> Group aggregate: min(pd.obs_date), max(pd.obs_date), count(pd.obs_date)  (actual time=0.444..2164.599 rows=35000 loops=1)
                                -> Nested loop inner join  (cost=632303.80 rows=784731) (actual time=0.379..2046.866 rows=798207 loops=1)
                                    -> Nested loop inner join  (cost=20748.06 rows=39624) (actual time=0.030..98.733 rows=40000 loops=1)
                                        -> Index scan on t using PRIMARY  (cost=3608.15 rows=34559) (actual time=0.017..12.509 rows=35000 loops=1)
                                        -> Index lookup on tp using YSE_App_transientpho_transient_id_9770bdf2_fk_YSE_App_t (transient_id=t.id)  (cost=0.38 rows=1) (actual time=0.002..0.002 rows=1 loops=35000)
                                    -> Filter: (((pd.flux / pd.flux_err) > 2) or (pd.mag_err < 0.35) or ((pd.mag_err is null) and (pd.mag is not null)))  (cost=13.45 rows=20) (actual time=0.043..0.047 rows=20 loops=40000)
                                        -> Index lookup on pd using YSE_App_transientpho_photometry_id_fbc5dfd2_fk_YSE_App_t (photometry_id=tp.id)  (cost=13.45 rows=20) (actual time=0.043..0.045 rows=20 loops=40000)

```
### before: new plan (411 ms)
```
-> Sort: g.first_detection DESC  (actual time=501.580..501.605 rows=451 loops=1)
    -> Table scan on <temporary>  (cost=2.50..2.50 rows=0) (actual time=0.001..0.064 rows=451 loops=1)
        -> Temporary table with deduplication  (cost=11.88..11.88 rows=0) (actual time=501.381..501.469 rows=451 loops=1)
            -> Nested loop inner join  (cost=9.38 rows=0) (actual time=499.890..501.124 rows=451 loops=1)
                -> Inner hash join (no condition)  (cost=3.88 rows=0) (actual time=499.866..499.961 rows=451 loops=1)
                    -> Table scan on g  (cost=2.50..2.50 rows=0) (actual time=0.001..0.022 rows=451 loops=1)
                        -> Materialize  (cost=2.50..2.50 rows=0) (actual time=499.781..499.830 rows=451 loops=1)
                            -> Filter: (((<cache>(to_days(curdate())) - to_days(min(pd.obs_date))) < 15) and ((<cache>(to_days(curdate())) - to_days(max(pd.obs_date))) < 2) and (count(pd.obs_date) > 2))  (actual time=499.384..499.708 rows=451 loops=1)
                                -> Table scan on <temporary>  (actual time=0.002..0.086 rows=1638 loops=1)
                                    -> Aggregate using temporary table  (actual time=499.350..499.524 rows=1638 loops=1)
                                        -> Nested loop inner join  (cost=898524.18 rows=5989386) (actual time=363.109..486.667 rows=37216 loops=1)
                                            -> Nested loop inner join  (cost=299569.66 rows=302427) (actual time=363.011..371.888 rows=1865 loops=1)
                                                -> Nested loop inner join  (cost=269326.71 rows=263768) (actual time=362.998..368.035 rows=1638 loops=1)
                                                    -> Table scan on <subquery3>  (cost=0.01..3299.60 rows=263768) (actual time=0.002..0.180 rows=1638 loops=1)
                                                        -> Materialize with deduplication  (cost=239650.05..242949.63 rows=263768) (actual time=362.980..363.289 rows=1638 loops=1)
                                                            -> Nested loop inner join  (cost=213273.21 rows=263768) (actual time=0.198..359.202 rows=19601 loops=1)
                                                                -> Filter: (pd2.obs_date >= <cache>((curdate() - interval 1 day)))  (cost=33309.78 rows=263768) (actual time=0.179..346.586 rows=19601 loops=1)
                                                                    -> Table scan on pd2  (cost=33309.78 rows=791384) (actual time=0.041..303.151 rows=800000 loops=1)
                                                                -> Single-row index lookup on tp2 using PRIMARY (id=pd2.photometry_id)  (cost=0.58 rows=1) (actual time=0.000..0.000 rows=1 loops=19601)
                                                    -> Single-row index lookup on t using PRIMARY (id=`<subquery3>`.transient_id)  (cost=0.35 rows=1) (actual time=0.003..0.003 rows=1 loops=1638)
                                                -> Index lookup on tp using YSE_App_transientpho_transient_id_9770bdf2_fk_YSE_App_t (transient_id=`<subquery3>`.transient_id)  (cost=0.36 rows=1) (actual time=0.002..0.002 rows=1 loops=1638)
                                            -> Filter: (((pd.flux / pd.flux_err) > 2) or (pd.mag_err < 0.35) or ((pd.mag_err is null) and (pd.mag is not null)))  (cost=15.59 rows=20) (actual time=0.056..0.060 rows=20 loops=1865)
                                                -> Index lookup on pd using YSE_App_transientpho_photometry_id_fbc5dfd2_fk_YSE_App_t (photometry_id=tp.id)  (cost=15.59 rows=20) (actual time=0.056..0.058 rows=20 loops=1865)
                    -> Hash
                        -> Table scan on og  (cost=1.10 rows=1) (actual time=0.063..0.065 rows=1 loops=1)
                -> Filter: ((t.obs_group_id = og.id) and ((t.TNS_spec_class <> 'SN Ia') or (t.TNS_spec_class is null)))  (cost=0.25 rows=1) (actual time=0.002..0.002 rows=1 loops=451)
                    -> Single-row index lookup on t using PRIMARY (id=g.id)  (cost=0.25 rows=1) (actual time=0.002..0.002 rows=1 loops=451)

```

### after: old plan (2579 ms)
```
-> Sort: g.first_detection DESC  (actual time=2614.156..2614.179 rows=451 loops=1)
    -> Table scan on <temporary>  (cost=2.50..2.50 rows=0) (actual time=0.003..0.055 rows=451 loops=1)
        -> Temporary table with deduplication  (cost=181228.12..181228.12 rows=0) (actual time=2613.976..2614.053 rows=451 loops=1)
            -> Nested loop inner join  (cost=181225.62 rows=0) (actual time=2579.988..2613.561 rows=451 loops=1)
                -> Inner hash join (t.obs_group_id = og.id)  (cost=3595.21 rows=28498) (actual time=0.717..18.024 rows=35000 loops=1)
                    -> Filter: ((t.TNS_spec_class <> 'SN Ia') or (t.TNS_spec_class is null))  (cost=3594.11 rows=31317) (actual time=0.022..13.691 rows=35000 loops=1)
                        -> Table scan on t  (cost=3594.11 rows=34414) (actual time=0.021..10.847 rows=35000 loops=1)
                    -> Hash
                        -> Table scan on og  (cost=1.10 rows=1) (actual time=0.680..0.682 rows=1 loops=1)
                -> Index lookup on g using <auto_key0> (id=t.id)  (actual time=0.000..0.000 rows=0 loops=35000)
                    -> Materialize  (cost=0.00..0.00 rows=0) (actual time=0.074..0.074 rows=0 loops=35000)
                        -> Filter: (((<cache>(to_days(curdate())) - to_days(min(pd.obs_date))) < 15) and ((<cache>(to_days(curdate())) - to_days(max(pd.obs_date))) < 2) and (count(pd.obs_date) > 2))  (actual time=3.000..2575.165 rows=451 loops=1)
                            -> Group aggregate: min(pd.obs_date), max(pd.obs_date), count(pd.obs_date)  (actual time=0.429..2566.030 rows=35000 loops=1)
                                -> Nested loop inner join  (cost=466072.69 rows=780794) (actual time=0.369..2464.581 rows=798207 loops=1)
                                    -> Nested loop inner join  (cost=20016.29 rows=39458) (actual time=0.028..101.182 rows=40000 loops=1)
                                        -> Index scan on t using PRIMARY  (cost=3593.65 rows=34414) (actual time=0.017..20.865 rows=35000 loops=1)
                                        -> Index lookup on tp using YSE_App_transientpho_transient_id_9770bdf2_fk_YSE_App_t (transient_id=t.id)  (cost=0.36 rows=1) (actual time=0.002..0.002 rows=1 loops=35000)
                                    -> Filter: (((pd.flux / pd.flux_err) > 2) or (pd.mag_err < 0.35) or ((pd.mag_err is null) and (pd.mag is not null)))  (cost=9.33 rows=20) (actual time=0.054..0.058 rows=20 loops=40000)
                                        -> Index lookup on pd using YSE_App_transientpho_photometry_id_fbc5dfd2_fk_YSE_App_t (photometry_id=tp.id)  (cost=9.33 rows=20) (actual time=0.054..0.056 rows=20 loops=40000)

```
### after: new plan (300 ms)
```
-> Sort: g.first_detection DESC  (actual time=429.500..429.548 rows=451 loops=1)
    -> Table scan on <temporary>  (cost=2.50..2.50 rows=0) (actual time=0.002..0.031 rows=451 loops=1)
        -> Temporary table with deduplication  (cost=11.16..11.16 rows=0) (actual time=429.335..429.389 rows=451 loops=1)
            -> Nested loop inner join  (cost=8.66 rows=0) (actual time=427.428..429.092 rows=451 loops=1)
                -> Inner hash join (no condition)  (cost=3.13 rows=0) (actual time=427.404..427.492 rows=451 loops=1)
                    -> Table scan on g  (cost=2.50..2.50 rows=0) (actual time=0.001..0.022 rows=451 loops=1)
                        -> Materialize  (cost=2.50..2.50 rows=0) (actual time=427.359..427.406 rows=451 loops=1)
                            -> Filter: (((<cache>(to_days(curdate())) - to_days(min(pd.obs_date))) < 15) and ((<cache>(to_days(curdate())) - to_days(max(pd.obs_date))) < 2) and (count(pd.obs_date) > 2))  (actual time=426.990..427.291 rows=451 loops=1)
                                -> Table scan on <temporary>  (actual time=0.002..0.065 rows=1638 loops=1)
                                    -> Aggregate using temporary table  (actual time=426.958..427.113 rows=1638 loops=1)
                                        -> Nested loop inner join  (cost=222686.02 rows=812107) (actual time=0.440..415.848 rows=37216 loops=1)
                                            -> Nested loop inner join  (cost=141462.27 rows=41040) (actual time=0.360..304.912 rows=1865 loops=1)
                                                -> Nested loop inner join  (cost=137357.99 rows=35794) (actual time=0.304..299.712 rows=1638 loops=1)
                                                    -> Nested loop semijoin with duplicate removal on YSE_App_transientpho_transient_id_9770bdf2_fk_YSE_App_t  (cost=124774.33 rows=35794) (actual time=0.297..293.544 rows=1638 loops=1)
                                                        -> Index scan on tp2 using YSE_App_transientpho_transient_id_9770bdf2_fk_YSE_App_t  (cost=3510.24 rows=40132) (actual time=0.019..11.020 rows=40000 loops=1)
                                                        -> Filter: (pd2.obs_date >= <cache>((curdate() - interval 1 day)))  (cost=1.04 rows=1) (actual time=0.007..0.007 rows=0 loops=39773)
                                                            -> Index lookup on pd2 using yse_photdata_phot_obs_idx (photometry_id=tp2.id)  (cost=1.04 rows=20) (actual time=0.003..0.006 rows=20 loops=39773)
                                                    -> Single-row index lookup on t using PRIMARY (id=tp2.transient_id)  (cost=9004.34 rows=1) (actual time=0.003..0.003 rows=1 loops=1638)
                                                -> Index lookup on tp using YSE_App_transientpho_transient_id_9770bdf2_fk_YSE_App_t (transient_id=tp2.transient_id)  (cost=0.36 rows=1) (actual time=0.002..0.003 rows=1 loops=1638)
                                            -> Filter: (((pd.flux / pd.flux_err) > 2) or (pd.mag_err < 0.35) or ((pd.mag_err is null) and (pd.mag is not null)))  (cost=13.08 rows=20) (actual time=0.054..0.058 rows=20 loops=1865)
                                                -> Index lookup on pd using YSE_App_transientpho_photometry_id_fbc5dfd2_fk_YSE_App_t (photometry_id=tp.id)  (cost=13.08 rows=20) (actual time=0.054..0.056 rows=20 loops=1865)
                    -> Hash
                        -> Table scan on og  (cost=0.35 rows=1) (actual time=0.025..0.028 rows=1 loops=1)
                -> Filter: ((t.obs_group_id = og.id) and ((t.TNS_spec_class <> 'SN Ia') or (t.TNS_spec_class is null)))  (cost=0.26 rows=1) (actual time=0.003..0.003 rows=1 loops=451)
                    -> Single-row index lookup on t using PRIMARY (id=g.id)  (cost=0.26 rows=1) (actual time=0.003..0.003 rows=1 loops=451)

```

## saved 11 Interesting New Targets

### before: old plan (3201 ms)
```
-> Sort: t.`name` DESC, pd.obs_date  (actual time=3543.010..3543.142 rows=799 loops=1)
    -> Table scan on <temporary>  (cost=2.50..2.50 rows=0) (actual time=0.003..0.056 rows=799 loops=1)
        -> Temporary table with deduplication  (cost=779380.87..779380.87 rows=0) (actual time=3542.631..3542.728 rows=799 loops=1)
            -> Nested loop inner join  (cost=779378.37 rows=0) (actual time=1954.566..3536.213 rows=799 loops=1)
                -> Nested loop inner join  (cost=111256.21 rows=130772) (actual time=3.572..1567.169 rows=18420 loops=1)
                    -> Nested loop inner join  (cost=10517.78 rows=6603) (actual time=0.260..136.418 rows=22070 loops=1)
                        -> Nested loop inner join  (cost=7661.50 rows=5759) (actual time=0.252..88.267 rows=19340 loops=1)
                            -> Nested loop inner join  (cost=2185.00 rows=5759) (actual time=0.054..42.522 rows=19340 loops=1)
                                -> Table scan on pb  (cost=0.35 rows=1) (actual time=0.020..0.024 rows=1 loops=1)
                                -> Filter: ((t.mw_ebv < 0.5) and (t.host_id is not null))  (cost=1032.66 rows=5759) (actual time=0.033..41.110 rows=19340 loops=1)
                                    -> Index lookup on t using YSE_App_transient_status_id_997b80b0_fk_YSE_App_t (status_id=1)  (cost=1032.66 rows=17279) (actual time=0.032..37.029 rows=19340 loops=1)
                            -> Single-row index lookup on h using PRIMARY (id=t.host_id)  (cost=0.85 rows=1) (actual time=0.002..0.002 rows=1 loops=19340)
                        -> Index lookup on tp using YSE_App_transientpho_transient_id_9770bdf2_fk_YSE_App_t (transient_id=t.id)  (cost=0.38 rows=1) (actual time=0.002..0.002 rows=1 loops=19340)
                    -> Filter: ((pd.band_id = pb.id) and ((pd.mag < 17) or ((coalesce(t.redshift,h.redshift) > 0) and (coalesce(t.redshift,h.redshift) <= 0.01) and ((((acos(((sin(radians(t.`dec`)) * sin(radians(h.`dec`))) + ((cos(radians(t.`dec`)) * cos(radians(h.`dec`))) * cos(radians(abs((t.ra - h.ra))))))) * ((3e+5 * coalesce(t.redshift,h.redshift)) / 73)) / pow((1.0 + coalesce(t.redshift,h.redshift)),2)) * 1000) < 40))) and (pd.mag is not null))  (cost=13.28 rows=20) (actual time=0.064..0.065 rows=1 loops=22070)
                        -> Index lookup on pd using YSE_App_transientpho_photometry_id_fbc5dfd2_fk_YSE_App_t (photometry_id=tp.id)  (cost=13.28 rows=20) (actual time=0.058..0.061 rows=20 loops=22070)
                -> Index lookup on g1 using <auto_key0> (transient_id=t.id, min_mag=pd.mag)  (actual time=0.001..0.001 rows=0 loops=18420)
                    -> Materialize  (cost=0.00..0.00 rows=0) (actual time=0.107..0.107 rows=0 loops=18420)
                        -> Group aggregate: min(a1.mag)  (actual time=0.137..1907.543 rows=35000 loops=1)
                            -> Nested loop inner join  (cost=625256.54 rows=706258) (actual time=0.074..1841.028 rows=800000 loops=1)
                                -> Nested loop inner join  (cost=20748.06 rows=39624) (actual time=0.034..93.106 rows=40000 loops=1)
                                    -> Index scan on a3 using PRIMARY  (cost=3608.15 rows=34559) (actual time=0.027..12.303 rows=35000 loops=1)
                                    -> Index lookup on a2 using YSE_App_transientpho_transient_id_9770bdf2_fk_YSE_App_t (transient_id=a3.id)  (cost=0.38 rows=1) (actual time=0.002..0.002 rows=1 loops=35000)
                                -> Filter: (a1.mag is not null)  (cost=13.28 rows=18) (actual time=0.039..0.042 rows=20 loops=40000)
                                    -> Index lookup on a1 using YSE_App_transientpho_photometry_id_fbc5dfd2_fk_YSE_App_t (photometry_id=a2.id)  (cost=13.28 rows=20) (actual time=0.039..0.041 rows=20 loops=40000)

```
### before: new plan (2746 ms)
```
-> Sort: t.`name` DESC, pd.obs_date  (actual time=2891.220..2891.288 rows=799 loops=1)
    -> Table scan on <temporary>  (cost=2.50..2.50 rows=0) (actual time=0.002..0.063 rows=799 loops=1)
        -> Temporary table with deduplication  (cost=445309.38..445309.38 rows=0) (actual time=2890.872..2890.977 rows=799 loops=1)
            -> Nested loop inner join  (cost=445306.88 rows=0) (actual time=1454.117..2885.055 rows=799 loops=1)
                -> Nested loop inner join  (cost=111256.21 rows=130772) (actual time=3.526..1417.474 rows=18420 loops=1)
                    -> Nested loop inner join  (cost=10517.78 rows=6603) (actual time=0.243..124.466 rows=22070 loops=1)
                        -> Nested loop inner join  (cost=7661.50 rows=5759) (actual time=0.234..81.624 rows=19340 loops=1)
                            -> Nested loop inner join  (cost=2185.00 rows=5759) (actual time=0.047..39.695 rows=19340 loops=1)
                                -> Table scan on pb  (cost=0.35 rows=1) (actual time=0.016..0.020 rows=1 loops=1)
                                -> Filter: ((t.mw_ebv < 0.5) and (t.host_id is not null))  (cost=1032.66 rows=5759) (actual time=0.030..38.263 rows=19340 loops=1)
                                    -> Index lookup on t using YSE_App_transient_status_id_997b80b0_fk_YSE_App_t (status_id=1)  (cost=1032.66 rows=17279) (actual time=0.029..34.266 rows=19340 loops=1)
                            -> Single-row index lookup on h using PRIMARY (id=t.host_id)  (cost=0.85 rows=1) (actual time=0.002..0.002 rows=1 loops=19340)
                        -> Index lookup on tp using YSE_App_transientpho_transient_id_9770bdf2_fk_YSE_App_t (transient_id=t.id)  (cost=0.38 rows=1) (actual time=0.002..0.002 rows=1 loops=19340)
                    -> Filter: ((pd.band_id = pb.id) and ((pd.mag < 17) or ((coalesce(t.redshift,h.redshift) > 0) and (coalesce(t.redshift,h.redshift) <= 0.01) and ((((acos(((sin(radians(t.`dec`)) * sin(radians(h.`dec`))) + ((cos(radians(t.`dec`)) * cos(radians(h.`dec`))) * cos(radians(abs((t.ra - h.ra))))))) * ((3e+5 * coalesce(t.redshift,h.redshift)) / 73)) / pow((1.0 + coalesce(t.redshift,h.redshift)),2)) * 1000) < 40))) and (pd.mag is not null))  (cost=13.28 rows=20) (actual time=0.058..0.058 rows=1 loops=22070)
                        -> Index lookup on pd using YSE_App_transientpho_photometry_id_fbc5dfd2_fk_YSE_App_t (photometry_id=tp.id)  (cost=13.28 rows=20) (actual time=0.053..0.055 rows=20 loops=22070)
                -> Index lookup on g1 using <auto_key0> (transient_id=t.id, min_mag=pd.mag)  (actual time=0.001..0.001 rows=0 loops=18420)
                    -> Materialize  (cost=0.00..0.00 rows=0) (actual time=0.079..0.079 rows=0 loops=18420)
                        -> Group aggregate: min(a1.mag)  (actual time=0.193..1428.083 rows=19340 loops=1)
                            -> Nested loop inner join  (cost=312549.02 rows=353119) (actual time=0.116..1388.601 rows=441400 loops=1)
                                -> Nested loop inner join  (cost=10303.52 rows=19811) (actual time=0.068..59.385 rows=22070 loops=1)
                                    -> Index lookup on a3 using YSE_App_transient_status_id_997b80b0_fk_YSE_App_t (status_id=1)  (cost=1733.81 rows=17279) (actual time=0.060..8.999 rows=19340 loops=1)
                                    -> Index lookup on a2 using YSE_App_transientpho_transient_id_9770bdf2_fk_YSE_App_t (transient_id=a3.id)  (cost=0.38 rows=1) (actual time=0.002..0.002 rows=1 loops=19340)
                                -> Filter: (a1.mag is not null)  (cost=13.28 rows=18) (actual time=0.056..0.059 rows=20 loops=22070)
                                    -> Index lookup on a1 using YSE_App_transientpho_photometry_id_fbc5dfd2_fk_YSE_App_t (photometry_id=a2.id)  (cost=13.28 rows=20) (actual time=0.055..0.057 rows=20 loops=22070)

```

### after: old plan (4147 ms)
```
-> Sort: t.`name` DESC, pd.obs_date  (actual time=5006.176..5006.244 rows=799 loops=1)
    -> Table scan on <temporary>  (cost=2.50..2.50 rows=0) (actual time=0.003..0.066 rows=799 loops=1)
        -> Temporary table with deduplication  (cost=747715.29..747715.29 rows=0) (actual time=5005.789..5005.896 rows=799 loops=1)
            -> Nested loop inner join  (cost=747712.79 rows=0) (actual time=2930.295..4999.429 rows=799 loops=1)
                -> Nested loop inner join  (cost=83472.59 rows=130119) (actual time=9.271..2057.721 rows=18420 loops=1)
                    -> Nested loop inner join  (cost=11281.49 rows=6576) (actual time=0.495..184.831 rows=22070 loops=1)
                        -> Nested loop inner join  (cost=8329.56 rows=5735) (actual time=0.304..137.755 rows=19340 loops=1)
                            -> Nested loop inner join  (cost=2368.27 rows=5735) (actual time=0.287..69.137 rows=19340 loops=1)
                                -> Table scan on pb  (cost=0.35 rows=1) (actual time=0.052..0.391 rows=1 loops=1)
                                -> Filter: ((t.mw_ebv < 0.5) and (t.host_id is not null))  (cost=1220.73 rows=5735) (actual time=0.234..67.325 rows=19340 loops=1)
                                    -> Index lookup on t using YSE_App_transient_status_id_997b80b0_fk_YSE_App_t (status_id=1)  (cost=1220.73 rows=17207) (actual time=0.231..63.638 rows=19340 loops=1)
                            -> Single-row index lookup on h using PRIMARY (id=t.host_id)  (cost=0.94 rows=1) (actual time=0.003..0.003 rows=1 loops=19340)
                        -> Index lookup on tp using YSE_App_transientpho_transient_id_9770bdf2_fk_YSE_App_t (transient_id=t.id)  (cost=0.40 rows=1) (actual time=0.002..0.002 rows=1 loops=19340)
                    -> Filter: ((pd.band_id = pb.id) and ((pd.mag < 17) or ((coalesce(t.redshift,h.redshift) > 0) and (coalesce(t.redshift,h.redshift) <= 0.01) and ((((acos(((sin(radians(t.`dec`)) * sin(radians(h.`dec`))) + ((cos(radians(t.`dec`)) * cos(radians(h.`dec`))) * cos(radians(abs((t.ra - h.ra))))))) * ((3e+5 * coalesce(t.redshift,h.redshift)) / 73)) / pow((1.0 + coalesce(t.redshift,h.redshift)),2)) * 1000) < 40))) and (pd.mag is not null))  (cost=9.00 rows=20) (actual time=0.084..0.085 rows=1 loops=22070)
                        -> Index lookup on pd using YSE_App_transientpho_photometry_id_fbc5dfd2_fk_YSE_App_t (photometry_id=tp.id)  (cost=9.00 rows=20) (actual time=0.079..0.081 rows=20 loops=22070)
                -> Index lookup on g1 using <auto_key0> (transient_id=t.id, min_mag=pd.mag)  (actual time=0.001..0.001 rows=0 loops=18420)
                    -> Materialize  (cost=0.00..0.00 rows=0) (actual time=0.159..0.160 rows=0 loops=18420)
                        -> Group aggregate: min(a1.mag)  (actual time=0.376..2870.504 rows=35000 loops=1)
                            -> Nested loop inner join  (cost=454560.40 rows=702714) (actual time=0.305..2793.903 rows=800000 loops=1)
                                -> Nested loop inner join  (cost=21370.49 rows=39458) (actual time=0.251..126.541 rows=40000 loops=1)
                                    -> Index scan on a3 using PRIMARY  (cost=3657.14 rows=34414) (actual time=0.170..36.831 rows=35000 loops=1)
                                    -> Index lookup on a2 using YSE_App_transientpho_transient_id_9770bdf2_fk_YSE_App_t (transient_id=a3.id)  (cost=0.40 rows=1) (actual time=0.002..0.002 rows=1 loops=35000)
                                -> Filter: (a1.mag is not null)  (cost=9.00 rows=18) (actual time=0.062..0.065 rows=20 loops=40000)
                                    -> Index lookup on a1 using YSE_App_transientpho_photometry_id_fbc5dfd2_fk_YSE_App_t (photometry_id=a2.id)  (cost=9.00 rows=20) (actual time=0.062..0.064 rows=20 loops=40000)

```
### after: new plan (3763 ms)
```
-> Sort: t.`name` DESC, pd.obs_date  (actual time=5110.317..5110.383 rows=799 loops=1)
    -> Table scan on <temporary>  (cost=2.50..2.50 rows=0) (actual time=0.004..0.057 rows=799 loops=1)
        -> Temporary table with deduplication  (cost=415822.09..415822.09 rows=0) (actual time=5109.983..5110.078 rows=799 loops=1)
            -> Nested loop inner join  (cost=415819.59 rows=0) (actual time=2428.828..5099.328 rows=799 loops=1)
                -> Nested loop inner join  (cost=83699.49 rows=130119) (actual time=10.198..2660.795 rows=18420 loops=1)
                    -> Nested loop inner join  (cost=11281.49 rows=6576) (actual time=0.539..245.819 rows=22070 loops=1)
                        -> Nested loop inner join  (cost=8329.56 rows=5735) (actual time=0.313..188.569 rows=19340 loops=1)
                            -> Nested loop inner join  (cost=2368.27 rows=5735) (actual time=0.078..87.620 rows=19340 loops=1)
                                -> Table scan on pb  (cost=0.35 rows=1) (actual time=0.016..0.392 rows=1 loops=1)
                                -> Filter: ((t.mw_ebv < 0.5) and (t.host_id is not null))  (cost=1220.73 rows=5735) (actual time=0.061..85.746 rows=19340 loops=1)
                                    -> Index lookup on t using YSE_App_transient_status_id_997b80b0_fk_YSE_App_t (status_id=1)  (cost=1220.73 rows=17207) (actual time=0.060..80.926 rows=19340 loops=1)
                            -> Single-row index lookup on h using PRIMARY (id=t.host_id)  (cost=0.94 rows=1) (actual time=0.005..0.005 rows=1 loops=19340)
                        -> Index lookup on tp using YSE_App_transientpho_transient_id_9770bdf2_fk_YSE_App_t (transient_id=t.id)  (cost=0.40 rows=1) (actual time=0.002..0.003 rows=1 loops=19340)
                    -> Filter: ((pd.band_id = pb.id) and ((pd.mag < 17) or ((coalesce(t.redshift,h.redshift) > 0) and (coalesce(t.redshift,h.redshift) <= 0.01) and ((((acos(((sin(radians(t.`dec`)) * sin(radians(h.`dec`))) + ((cos(radians(t.`dec`)) * cos(radians(h.`dec`))) * cos(radians(abs((t.ra - h.ra))))))) * ((3e+5 * coalesce(t.redshift,h.redshift)) / 73)) / pow((1.0 + coalesce(t.redshift,h.redshift)),2)) * 1000) < 40))) and (pd.mag is not null))  (cost=9.03 rows=20) (actual time=0.109..0.109 rows=1 loops=22070)
                        -> Index lookup on pd using YSE_App_transientpho_photometry_id_fbc5dfd2_fk_YSE_App_t (photometry_id=tp.id)  (cost=9.03 rows=20) (actual time=0.102..0.105 rows=20 loops=22070)
                -> Index lookup on g1 using <auto_key0> (transient_id=t.id, min_mag=pd.mag)  (actual time=0.001..0.001 rows=0 loops=18420)
                    -> Materialize  (cost=0.00..0.00 rows=0) (actual time=0.132..0.132 rows=0 loops=18420)
                        -> Group aggregate: min(a1.mag)  (actual time=0.370..2388.540 rows=19340 loops=1)
                            -> Nested loop inner join  (cost=227189.40 rows=351357) (actual time=0.300..2342.473 rows=441400 loops=1)
                                -> Nested loop inner join  (cost=10594.44 rows=19729) (actual time=0.088..67.539 rows=22070 loops=1)
                                    -> Index lookup on a3 using YSE_App_transient_status_id_997b80b0_fk_YSE_App_t (status_id=1)  (cost=1737.77 rows=17207) (actual time=0.024..9.774 rows=19340 loops=1)
                                    -> Index lookup on a2 using YSE_App_transientpho_transient_id_9770bdf2_fk_YSE_App_t (transient_id=a3.id)  (cost=0.40 rows=1) (actual time=0.002..0.003 rows=1 loops=19340)
                                -> Filter: (a1.mag is not null)  (cost=9.00 rows=18) (actual time=0.098..0.102 rows=20 loops=22070)
                                    -> Index lookup on a1 using YSE_App_transientpho_photometry_id_fbc5dfd2_fk_YSE_App_t (photometry_id=a2.id)  (cost=9.00 rows=20) (actual time=0.098..0.100 rows=20 loops=22070)

```

## code recent_mag annotation (table_utils/yse_views/views), 2000 rows

### before: old plan (466 ms)
```
-> Filter: (YSE_App_transient.id in (17,34,51,68,85,102,119,136,153,170,187,204,221,238,255,272,289,306,323,340,357,374,391,408,425,442,459,476,493,510,527,544,561,578,595,612,629,646,663,680,697,714,731,748,765,782,799,816,833,850,867,884,901,918,935,952,969,986,1003,1020,1037,1054,1071,1088,1105,1122,1139,1156,1173,1190,1207,1224,1241,1258,1275,1292,1309,1326,1343,1360,1377,1394,1411,1428,1445,1462,1479,1496,1513,1530,1547,1564,1581,1598,1615,1632,1649,1666,1683,1700,1717,1734,1751,1768,1785,1802,1819,1836,1853,1870,1887,1904,1921,1938,1955,1972,1989,2006,2023,2040,2057,2074,2091,2108,2125,2142,2159,2176,2193,2210,2227,2244,2261,2278,2295,2312,2329,2346,2363,2380,2397,2414,2431,2448,2465,2482,2499,2516,2533,2550,2567,2584,2601,2618,2635,2652,2669,2686,2703,2720,2737,2754,2771,2788,2805,2822,2839,2856,2873,2890,2907,2924,2941,2958,2975,2992,3009,3026,3043,3060,3077,3094,3111,3128,3145,3162,3179,3196,3213,3230,3247,3264,3281,3298,3315,3332,3349,3366,3383,3400,3417,3434,3451,3468,3485,3502,3519,3536,3553,3570,3587,3604,3621,3638,3655,3672,3689,3706,3723,3740,3757,3774,3791,3808,3825,3842,3859,3876,3893,3910,3927,3944,3961,3978,3995,4012,4029,4046,4063,4080,4097,4114,4131,4148,4165,4182,4199,4216,4233,4250,4267,4284,4301,4318,4335,4352,4369,4386,4403,4420,4437,4454,4471,4488,4505,4522,4539,4556,4573,4590,4607,4624,4641,4658,4675,4692,4709,4726,4743,4760,4777,4794,4811,4828,4845,4862,4879,4896,4913,4930,4947,4964,4981,4998,5015,5032,5049,5066,5083,5100,5117,5134,5151,5168,5185,5202,5219,5236,5253,5270,5287,5304,5321,5338,5355,5372,5389,5406,5423,5440,5457,5474,5491,5508,5525,5542,5559,5576,5593,5610,5627,5644,5661,5678,5695,5712,5729,5746,5763,5780,5797,5814,5831,5848,5865,5882,5899,5916,5933,5950,5967,5984,6001,6018,6035,6052,6069,6086,6103,6120,6137,6154,6171,6188,6205,6222,6239,6256,6273,6290,6307,6324,6341,6358,6375,6392,6409,6426,6443,6460,6477,6494,6511,6528,6545,6562,6579,6596,6613,6630,6647,6664,6681,6698,6715,6732,6749,6766,6783,6800,6817,6834,6851,6868,6885,6902,6919,6936,6953,6970,6987,7004,7021,7038,7055,7072,7089,7106,7123,7140,7157,7174,7191,7208,7225,7242,7259,7276,7293,7310,7327,7344,7361,7378,7395,7412,7429,7446,7463,7480,7497,7514,7531,7548,7565,7582,7599,7616,7633,7650,7667,7684,7701,7718,7735,7752,7769,7786,7803,7820,7837,7854,7871,7888,7905,7922,7939,7956,7973,7990,8007,8024,8041,8058,8075,8092,8109,8126,8143,8160,8177,8194,8211,8228,8245,8262,8279,8296,8313,8330,8347,8364,8381,8398,8415,8432,8449,8466,8483,8500,8517,8534,8551,8568,8585,8602,8619,8636,8653,8670,8687,8704,8721,8738,8755,8772,8789,8806,8823,8840,8857,8874,8891,8908,8925,8942,8959,8976,8993,9010,9027,9044,9061,9078,9095,9112,9129,9146,9163,9180,9197,9214,9231,9248,9265,9282,9299,9316,9333,9350,9367,9384,9401,9418,9435,9452,9469,9486,9503,9520,9537,9554,9571,9588,9605,9622,9639,9656,9673,9690,9707,9724,9741,9758,9775,9792,9809,9826,9843,9860,9877,9894,9911,9928,9945,9962,9979,9996,10013,10030,10047,10064,10081,10098,10115,10132,10149,10166,10183,10200,10217,10234,10251,10268,10285,10302,10319,10336,10353,10370,10387,10404,10421,10438,10455,10472,10489,10506,10523,10540,10557,10574,10591,10608,10625,10642,10659,10676,10693,10710,10727,10744,10761,10778,10795,10812,10829,10846,10863,10880,10897,10914,10931,10948,10965,10982,10999,11016,11033,11050,11067,11084,11101,11118,11135,11152,11169,11186,11203,11220,11237,11254,11271,11288,11305,11322,11339,11356,11373,11390,11407,11424,11441,11458,11475,11492,11509,11526,11543,11560,11577,11594,11611,11628,11645,11662,11679,11696,11713,11730,11747,11764,11781,11798,11815,11832,11849,11866,11883,11900,11917,11934,11951,11968,11985,12002,12019,12036,12053,12070,12087,12104,12121,12138,12155,12172,12189,12206,12223,12240,12257,12274,12291,12308,12325,12342,12359,12376,12393,12410,12427,12444,12461,12478,12495,12512,12529,12546,12563,12580,12597,12614,12631,12648,12665,12682,12699,12716,12733,12750,12767,12784,12801,12818,12835,12852,12869,12886,12903,12920,12937,12954,12971,12988,13005,13022,13039,13056,13073,13090,13107,13124,13141,13158,13175,13192,13209,13226,13243,13260,13277,13294,13311,13328,13345,13362,13379,13396,13413,13430,13447,13464,13481,13498,13515,13532,13549,13566,13583,13600,13617,13634,13651,13668,13685,13702,13719,13736,13753,13770,13787,13804,13821,13838,13855,13872,13889,13906,13923,13940,13957,13974,13991,14008,14025,14042,14059,14076,14093,14110,14127,14144,14161,14178,14195,14212,14229,14246,14263,14280,14297,14314,14331,14348,14365,14382,14399,14416,14433,14450,14467,14484,14501,14518,14535,14552,14569,14586,14603,14620,14637,14654,14671,14688,14705,14722,14739,14756,14773,14790,14807,14824,14841,14858,14875,14892,14909,14926,14943,14960,14977,14994,15011,15028,15045,15062,15079,15096,15113,15130,15147,15164,15181,15198,15215,15232,15249,15266,15283,15300,15317,15334,15351,15368,15385,15402,15419,15436,15453,15470,15487,15504,15521,15538,15555,15572,15589,15606,15623,15640,15657,15674,15691,15708,15725,15742,15759,15776,15793,15810,15827,15844,15861,15878,15895,1591
```
### before: new plan (309 ms)
```
-> Filter: (YSE_App_transient.id in (17,34,51,68,85,102,119,136,153,170,187,204,221,238,255,272,289,306,323,340,357,374,391,408,425,442,459,476,493,510,527,544,561,578,595,612,629,646,663,680,697,714,731,748,765,782,799,816,833,850,867,884,901,918,935,952,969,986,1003,1020,1037,1054,1071,1088,1105,1122,1139,1156,1173,1190,1207,1224,1241,1258,1275,1292,1309,1326,1343,1360,1377,1394,1411,1428,1445,1462,1479,1496,1513,1530,1547,1564,1581,1598,1615,1632,1649,1666,1683,1700,1717,1734,1751,1768,1785,1802,1819,1836,1853,1870,1887,1904,1921,1938,1955,1972,1989,2006,2023,2040,2057,2074,2091,2108,2125,2142,2159,2176,2193,2210,2227,2244,2261,2278,2295,2312,2329,2346,2363,2380,2397,2414,2431,2448,2465,2482,2499,2516,2533,2550,2567,2584,2601,2618,2635,2652,2669,2686,2703,2720,2737,2754,2771,2788,2805,2822,2839,2856,2873,2890,2907,2924,2941,2958,2975,2992,3009,3026,3043,3060,3077,3094,3111,3128,3145,3162,3179,3196,3213,3230,3247,3264,3281,3298,3315,3332,3349,3366,3383,3400,3417,3434,3451,3468,3485,3502,3519,3536,3553,3570,3587,3604,3621,3638,3655,3672,3689,3706,3723,3740,3757,3774,3791,3808,3825,3842,3859,3876,3893,3910,3927,3944,3961,3978,3995,4012,4029,4046,4063,4080,4097,4114,4131,4148,4165,4182,4199,4216,4233,4250,4267,4284,4301,4318,4335,4352,4369,4386,4403,4420,4437,4454,4471,4488,4505,4522,4539,4556,4573,4590,4607,4624,4641,4658,4675,4692,4709,4726,4743,4760,4777,4794,4811,4828,4845,4862,4879,4896,4913,4930,4947,4964,4981,4998,5015,5032,5049,5066,5083,5100,5117,5134,5151,5168,5185,5202,5219,5236,5253,5270,5287,5304,5321,5338,5355,5372,5389,5406,5423,5440,5457,5474,5491,5508,5525,5542,5559,5576,5593,5610,5627,5644,5661,5678,5695,5712,5729,5746,5763,5780,5797,5814,5831,5848,5865,5882,5899,5916,5933,5950,5967,5984,6001,6018,6035,6052,6069,6086,6103,6120,6137,6154,6171,6188,6205,6222,6239,6256,6273,6290,6307,6324,6341,6358,6375,6392,6409,6426,6443,6460,6477,6494,6511,6528,6545,6562,6579,6596,6613,6630,6647,6664,6681,6698,6715,6732,6749,6766,6783,6800,6817,6834,6851,6868,6885,6902,6919,6936,6953,6970,6987,7004,7021,7038,7055,7072,7089,7106,7123,7140,7157,7174,7191,7208,7225,7242,7259,7276,7293,7310,7327,7344,7361,7378,7395,7412,7429,7446,7463,7480,7497,7514,7531,7548,7565,7582,7599,7616,7633,7650,7667,7684,7701,7718,7735,7752,7769,7786,7803,7820,7837,7854,7871,7888,7905,7922,7939,7956,7973,7990,8007,8024,8041,8058,8075,8092,8109,8126,8143,8160,8177,8194,8211,8228,8245,8262,8279,8296,8313,8330,8347,8364,8381,8398,8415,8432,8449,8466,8483,8500,8517,8534,8551,8568,8585,8602,8619,8636,8653,8670,8687,8704,8721,8738,8755,8772,8789,8806,8823,8840,8857,8874,8891,8908,8925,8942,8959,8976,8993,9010,9027,9044,9061,9078,9095,9112,9129,9146,9163,9180,9197,9214,9231,9248,9265,9282,9299,9316,9333,9350,9367,9384,9401,9418,9435,9452,9469,9486,9503,9520,9537,9554,9571,9588,9605,9622,9639,9656,9673,9690,9707,9724,9741,9758,9775,9792,9809,9826,9843,9860,9877,9894,9911,9928,9945,9962,9979,9996,10013,10030,10047,10064,10081,10098,10115,10132,10149,10166,10183,10200,10217,10234,10251,10268,10285,10302,10319,10336,10353,10370,10387,10404,10421,10438,10455,10472,10489,10506,10523,10540,10557,10574,10591,10608,10625,10642,10659,10676,10693,10710,10727,10744,10761,10778,10795,10812,10829,10846,10863,10880,10897,10914,10931,10948,10965,10982,10999,11016,11033,11050,11067,11084,11101,11118,11135,11152,11169,11186,11203,11220,11237,11254,11271,11288,11305,11322,11339,11356,11373,11390,11407,11424,11441,11458,11475,11492,11509,11526,11543,11560,11577,11594,11611,11628,11645,11662,11679,11696,11713,11730,11747,11764,11781,11798,11815,11832,11849,11866,11883,11900,11917,11934,11951,11968,11985,12002,12019,12036,12053,12070,12087,12104,12121,12138,12155,12172,12189,12206,12223,12240,12257,12274,12291,12308,12325,12342,12359,12376,12393,12410,12427,12444,12461,12478,12495,12512,12529,12546,12563,12580,12597,12614,12631,12648,12665,12682,12699,12716,12733,12750,12767,12784,12801,12818,12835,12852,12869,12886,12903,12920,12937,12954,12971,12988,13005,13022,13039,13056,13073,13090,13107,13124,13141,13158,13175,13192,13209,13226,13243,13260,13277,13294,13311,13328,13345,13362,13379,13396,13413,13430,13447,13464,13481,13498,13515,13532,13549,13566,13583,13600,13617,13634,13651,13668,13685,13702,13719,13736,13753,13770,13787,13804,13821,13838,13855,13872,13889,13906,13923,13940,13957,13974,13991,14008,14025,14042,14059,14076,14093,14110,14127,14144,14161,14178,14195,14212,14229,14246,14263,14280,14297,14314,14331,14348,14365,14382,14399,14416,14433,14450,14467,14484,14501,14518,14535,14552,14569,14586,14603,14620,14637,14654,14671,14688,14705,14722,14739,14756,14773,14790,14807,14824,14841,14858,14875,14892,14909,14926,14943,14960,14977,14994,15011,15028,15045,15062,15079,15096,15113,15130,15147,15164,15181,15198,15215,15232,15249,15266,15283,15300,15317,15334,15351,15368,15385,15402,15419,15436,15453,15470,15487,15504,15521,15538,15555,15572,15589,15606,15623,15640,15657,15674,15691,15708,15725,15742,15759,15776,15793,15810,15827,15844,15861,15878,15895,1591
```

### after: old plan (183 ms)
```
-> Filter: (YSE_App_transient.id in (17,34,51,68,85,102,119,136,153,170,187,204,221,238,255,272,289,306,323,340,357,374,391,408,425,442,459,476,493,510,527,544,561,578,595,612,629,646,663,680,697,714,731,748,765,782,799,816,833,850,867,884,901,918,935,952,969,986,1003,1020,1037,1054,1071,1088,1105,1122,1139,1156,1173,1190,1207,1224,1241,1258,1275,1292,1309,1326,1343,1360,1377,1394,1411,1428,1445,1462,1479,1496,1513,1530,1547,1564,1581,1598,1615,1632,1649,1666,1683,1700,1717,1734,1751,1768,1785,1802,1819,1836,1853,1870,1887,1904,1921,1938,1955,1972,1989,2006,2023,2040,2057,2074,2091,2108,2125,2142,2159,2176,2193,2210,2227,2244,2261,2278,2295,2312,2329,2346,2363,2380,2397,2414,2431,2448,2465,2482,2499,2516,2533,2550,2567,2584,2601,2618,2635,2652,2669,2686,2703,2720,2737,2754,2771,2788,2805,2822,2839,2856,2873,2890,2907,2924,2941,2958,2975,2992,3009,3026,3043,3060,3077,3094,3111,3128,3145,3162,3179,3196,3213,3230,3247,3264,3281,3298,3315,3332,3349,3366,3383,3400,3417,3434,3451,3468,3485,3502,3519,3536,3553,3570,3587,3604,3621,3638,3655,3672,3689,3706,3723,3740,3757,3774,3791,3808,3825,3842,3859,3876,3893,3910,3927,3944,3961,3978,3995,4012,4029,4046,4063,4080,4097,4114,4131,4148,4165,4182,4199,4216,4233,4250,4267,4284,4301,4318,4335,4352,4369,4386,4403,4420,4437,4454,4471,4488,4505,4522,4539,4556,4573,4590,4607,4624,4641,4658,4675,4692,4709,4726,4743,4760,4777,4794,4811,4828,4845,4862,4879,4896,4913,4930,4947,4964,4981,4998,5015,5032,5049,5066,5083,5100,5117,5134,5151,5168,5185,5202,5219,5236,5253,5270,5287,5304,5321,5338,5355,5372,5389,5406,5423,5440,5457,5474,5491,5508,5525,5542,5559,5576,5593,5610,5627,5644,5661,5678,5695,5712,5729,5746,5763,5780,5797,5814,5831,5848,5865,5882,5899,5916,5933,5950,5967,5984,6001,6018,6035,6052,6069,6086,6103,6120,6137,6154,6171,6188,6205,6222,6239,6256,6273,6290,6307,6324,6341,6358,6375,6392,6409,6426,6443,6460,6477,6494,6511,6528,6545,6562,6579,6596,6613,6630,6647,6664,6681,6698,6715,6732,6749,6766,6783,6800,6817,6834,6851,6868,6885,6902,6919,6936,6953,6970,6987,7004,7021,7038,7055,7072,7089,7106,7123,7140,7157,7174,7191,7208,7225,7242,7259,7276,7293,7310,7327,7344,7361,7378,7395,7412,7429,7446,7463,7480,7497,7514,7531,7548,7565,7582,7599,7616,7633,7650,7667,7684,7701,7718,7735,7752,7769,7786,7803,7820,7837,7854,7871,7888,7905,7922,7939,7956,7973,7990,8007,8024,8041,8058,8075,8092,8109,8126,8143,8160,8177,8194,8211,8228,8245,8262,8279,8296,8313,8330,8347,8364,8381,8398,8415,8432,8449,8466,8483,8500,8517,8534,8551,8568,8585,8602,8619,8636,8653,8670,8687,8704,8721,8738,8755,8772,8789,8806,8823,8840,8857,8874,8891,8908,8925,8942,8959,8976,8993,9010,9027,9044,9061,9078,9095,9112,9129,9146,9163,9180,9197,9214,9231,9248,9265,9282,9299,9316,9333,9350,9367,9384,9401,9418,9435,9452,9469,9486,9503,9520,9537,9554,9571,9588,9605,9622,9639,9656,9673,9690,9707,9724,9741,9758,9775,9792,9809,9826,9843,9860,9877,9894,9911,9928,9945,9962,9979,9996,10013,10030,10047,10064,10081,10098,10115,10132,10149,10166,10183,10200,10217,10234,10251,10268,10285,10302,10319,10336,10353,10370,10387,10404,10421,10438,10455,10472,10489,10506,10523,10540,10557,10574,10591,10608,10625,10642,10659,10676,10693,10710,10727,10744,10761,10778,10795,10812,10829,10846,10863,10880,10897,10914,10931,10948,10965,10982,10999,11016,11033,11050,11067,11084,11101,11118,11135,11152,11169,11186,11203,11220,11237,11254,11271,11288,11305,11322,11339,11356,11373,11390,11407,11424,11441,11458,11475,11492,11509,11526,11543,11560,11577,11594,11611,11628,11645,11662,11679,11696,11713,11730,11747,11764,11781,11798,11815,11832,11849,11866,11883,11900,11917,11934,11951,11968,11985,12002,12019,12036,12053,12070,12087,12104,12121,12138,12155,12172,12189,12206,12223,12240,12257,12274,12291,12308,12325,12342,12359,12376,12393,12410,12427,12444,12461,12478,12495,12512,12529,12546,12563,12580,12597,12614,12631,12648,12665,12682,12699,12716,12733,12750,12767,12784,12801,12818,12835,12852,12869,12886,12903,12920,12937,12954,12971,12988,13005,13022,13039,13056,13073,13090,13107,13124,13141,13158,13175,13192,13209,13226,13243,13260,13277,13294,13311,13328,13345,13362,13379,13396,13413,13430,13447,13464,13481,13498,13515,13532,13549,13566,13583,13600,13617,13634,13651,13668,13685,13702,13719,13736,13753,13770,13787,13804,13821,13838,13855,13872,13889,13906,13923,13940,13957,13974,13991,14008,14025,14042,14059,14076,14093,14110,14127,14144,14161,14178,14195,14212,14229,14246,14263,14280,14297,14314,14331,14348,14365,14382,14399,14416,14433,14450,14467,14484,14501,14518,14535,14552,14569,14586,14603,14620,14637,14654,14671,14688,14705,14722,14739,14756,14773,14790,14807,14824,14841,14858,14875,14892,14909,14926,14943,14960,14977,14994,15011,15028,15045,15062,15079,15096,15113,15130,15147,15164,15181,15198,15215,15232,15249,15266,15283,15300,15317,15334,15351,15368,15385,15402,15419,15436,15453,15470,15487,15504,15521,15538,15555,15572,15589,15606,15623,15640,15657,15674,15691,15708,15725,15742,15759,15776,15793,15810,15827,15844,15861,15878,15895,1591
```
### after: new plan (135 ms)
```
-> Filter: (YSE_App_transient.id in (17,34,51,68,85,102,119,136,153,170,187,204,221,238,255,272,289,306,323,340,357,374,391,408,425,442,459,476,493,510,527,544,561,578,595,612,629,646,663,680,697,714,731,748,765,782,799,816,833,850,867,884,901,918,935,952,969,986,1003,1020,1037,1054,1071,1088,1105,1122,1139,1156,1173,1190,1207,1224,1241,1258,1275,1292,1309,1326,1343,1360,1377,1394,1411,1428,1445,1462,1479,1496,1513,1530,1547,1564,1581,1598,1615,1632,1649,1666,1683,1700,1717,1734,1751,1768,1785,1802,1819,1836,1853,1870,1887,1904,1921,1938,1955,1972,1989,2006,2023,2040,2057,2074,2091,2108,2125,2142,2159,2176,2193,2210,2227,2244,2261,2278,2295,2312,2329,2346,2363,2380,2397,2414,2431,2448,2465,2482,2499,2516,2533,2550,2567,2584,2601,2618,2635,2652,2669,2686,2703,2720,2737,2754,2771,2788,2805,2822,2839,2856,2873,2890,2907,2924,2941,2958,2975,2992,3009,3026,3043,3060,3077,3094,3111,3128,3145,3162,3179,3196,3213,3230,3247,3264,3281,3298,3315,3332,3349,3366,3383,3400,3417,3434,3451,3468,3485,3502,3519,3536,3553,3570,3587,3604,3621,3638,3655,3672,3689,3706,3723,3740,3757,3774,3791,3808,3825,3842,3859,3876,3893,3910,3927,3944,3961,3978,3995,4012,4029,4046,4063,4080,4097,4114,4131,4148,4165,4182,4199,4216,4233,4250,4267,4284,4301,4318,4335,4352,4369,4386,4403,4420,4437,4454,4471,4488,4505,4522,4539,4556,4573,4590,4607,4624,4641,4658,4675,4692,4709,4726,4743,4760,4777,4794,4811,4828,4845,4862,4879,4896,4913,4930,4947,4964,4981,4998,5015,5032,5049,5066,5083,5100,5117,5134,5151,5168,5185,5202,5219,5236,5253,5270,5287,5304,5321,5338,5355,5372,5389,5406,5423,5440,5457,5474,5491,5508,5525,5542,5559,5576,5593,5610,5627,5644,5661,5678,5695,5712,5729,5746,5763,5780,5797,5814,5831,5848,5865,5882,5899,5916,5933,5950,5967,5984,6001,6018,6035,6052,6069,6086,6103,6120,6137,6154,6171,6188,6205,6222,6239,6256,6273,6290,6307,6324,6341,6358,6375,6392,6409,6426,6443,6460,6477,6494,6511,6528,6545,6562,6579,6596,6613,6630,6647,6664,6681,6698,6715,6732,6749,6766,6783,6800,6817,6834,6851,6868,6885,6902,6919,6936,6953,6970,6987,7004,7021,7038,7055,7072,7089,7106,7123,7140,7157,7174,7191,7208,7225,7242,7259,7276,7293,7310,7327,7344,7361,7378,7395,7412,7429,7446,7463,7480,7497,7514,7531,7548,7565,7582,7599,7616,7633,7650,7667,7684,7701,7718,7735,7752,7769,7786,7803,7820,7837,7854,7871,7888,7905,7922,7939,7956,7973,7990,8007,8024,8041,8058,8075,8092,8109,8126,8143,8160,8177,8194,8211,8228,8245,8262,8279,8296,8313,8330,8347,8364,8381,8398,8415,8432,8449,8466,8483,8500,8517,8534,8551,8568,8585,8602,8619,8636,8653,8670,8687,8704,8721,8738,8755,8772,8789,8806,8823,8840,8857,8874,8891,8908,8925,8942,8959,8976,8993,9010,9027,9044,9061,9078,9095,9112,9129,9146,9163,9180,9197,9214,9231,9248,9265,9282,9299,9316,9333,9350,9367,9384,9401,9418,9435,9452,9469,9486,9503,9520,9537,9554,9571,9588,9605,9622,9639,9656,9673,9690,9707,9724,9741,9758,9775,9792,9809,9826,9843,9860,9877,9894,9911,9928,9945,9962,9979,9996,10013,10030,10047,10064,10081,10098,10115,10132,10149,10166,10183,10200,10217,10234,10251,10268,10285,10302,10319,10336,10353,10370,10387,10404,10421,10438,10455,10472,10489,10506,10523,10540,10557,10574,10591,10608,10625,10642,10659,10676,10693,10710,10727,10744,10761,10778,10795,10812,10829,10846,10863,10880,10897,10914,10931,10948,10965,10982,10999,11016,11033,11050,11067,11084,11101,11118,11135,11152,11169,11186,11203,11220,11237,11254,11271,11288,11305,11322,11339,11356,11373,11390,11407,11424,11441,11458,11475,11492,11509,11526,11543,11560,11577,11594,11611,11628,11645,11662,11679,11696,11713,11730,11747,11764,11781,11798,11815,11832,11849,11866,11883,11900,11917,11934,11951,11968,11985,12002,12019,12036,12053,12070,12087,12104,12121,12138,12155,12172,12189,12206,12223,12240,12257,12274,12291,12308,12325,12342,12359,12376,12393,12410,12427,12444,12461,12478,12495,12512,12529,12546,12563,12580,12597,12614,12631,12648,12665,12682,12699,12716,12733,12750,12767,12784,12801,12818,12835,12852,12869,12886,12903,12920,12937,12954,12971,12988,13005,13022,13039,13056,13073,13090,13107,13124,13141,13158,13175,13192,13209,13226,13243,13260,13277,13294,13311,13328,13345,13362,13379,13396,13413,13430,13447,13464,13481,13498,13515,13532,13549,13566,13583,13600,13617,13634,13651,13668,13685,13702,13719,13736,13753,13770,13787,13804,13821,13838,13855,13872,13889,13906,13923,13940,13957,13974,13991,14008,14025,14042,14059,14076,14093,14110,14127,14144,14161,14178,14195,14212,14229,14246,14263,14280,14297,14314,14331,14348,14365,14382,14399,14416,14433,14450,14467,14484,14501,14518,14535,14552,14569,14586,14603,14620,14637,14654,14671,14688,14705,14722,14739,14756,14773,14790,14807,14824,14841,14858,14875,14892,14909,14926,14943,14960,14977,14994,15011,15028,15045,15062,15079,15096,15113,15130,15147,15164,15181,15198,15215,15232,15249,15266,15283,15300,15317,15334,15351,15368,15385,15402,15419,15436,15453,15470,15487,15504,15521,15538,15555,15572,15589,15606,15623,15640,15657,15674,15691,15708,15725,15742,15759,15776,15793,15810,15827,15844,15861,15878,15895,1591
```

## code days_since_disc annotation (yse_views), 2000 rows

### before: old plan (18 ms)
```
-> Filter: (YSE_App_transient.id in (17,34,51,68,85,102,119,136,153,170,187,204,221,238,255,272,289,306,323,340,357,374,391,408,425,442,459,476,493,510,527,544,561,578,595,612,629,646,663,680,697,714,731,748,765,782,799,816,833,850,867,884,901,918,935,952,969,986,1003,1020,1037,1054,1071,1088,1105,1122,1139,1156,1173,1190,1207,1224,1241,1258,1275,1292,1309,1326,1343,1360,1377,1394,1411,1428,1445,1462,1479,1496,1513,1530,1547,1564,1581,1598,1615,1632,1649,1666,1683,1700,1717,1734,1751,1768,1785,1802,1819,1836,1853,1870,1887,1904,1921,1938,1955,1972,1989,2006,2023,2040,2057,2074,2091,2108,2125,2142,2159,2176,2193,2210,2227,2244,2261,2278,2295,2312,2329,2346,2363,2380,2397,2414,2431,2448,2465,2482,2499,2516,2533,2550,2567,2584,2601,2618,2635,2652,2669,2686,2703,2720,2737,2754,2771,2788,2805,2822,2839,2856,2873,2890,2907,2924,2941,2958,2975,2992,3009,3026,3043,3060,3077,3094,3111,3128,3145,3162,3179,3196,3213,3230,3247,3264,3281,3298,3315,3332,3349,3366,3383,3400,3417,3434,3451,3468,3485,3502,3519,3536,3553,3570,3587,3604,3621,3638,3655,3672,3689,3706,3723,3740,3757,3774,3791,3808,3825,3842,3859,3876,3893,3910,3927,3944,3961,3978,3995,4012,4029,4046,4063,4080,4097,4114,4131,4148,4165,4182,4199,4216,4233,4250,4267,4284,4301,4318,4335,4352,4369,4386,4403,4420,4437,4454,4471,4488,4505,4522,4539,4556,4573,4590,4607,4624,4641,4658,4675,4692,4709,4726,4743,4760,4777,4794,4811,4828,4845,4862,4879,4896,4913,4930,4947,4964,4981,4998,5015,5032,5049,5066,5083,5100,5117,5134,5151,5168,5185,5202,5219,5236,5253,5270,5287,5304,5321,5338,5355,5372,5389,5406,5423,5440,5457,5474,5491,5508,5525,5542,5559,5576,5593,5610,5627,5644,5661,5678,5695,5712,5729,5746,5763,5780,5797,5814,5831,5848,5865,5882,5899,5916,5933,5950,5967,5984,6001,6018,6035,6052,6069,6086,6103,6120,6137,6154,6171,6188,6205,6222,6239,6256,6273,6290,6307,6324,6341,6358,6375,6392,6409,6426,6443,6460,6477,6494,6511,6528,6545,6562,6579,6596,6613,6630,6647,6664,6681,6698,6715,6732,6749,6766,6783,6800,6817,6834,6851,6868,6885,6902,6919,6936,6953,6970,6987,7004,7021,7038,7055,7072,7089,7106,7123,7140,7157,7174,7191,7208,7225,7242,7259,7276,7293,7310,7327,7344,7361,7378,7395,7412,7429,7446,7463,7480,7497,7514,7531,7548,7565,7582,7599,7616,7633,7650,7667,7684,7701,7718,7735,7752,7769,7786,7803,7820,7837,7854,7871,7888,7905,7922,7939,7956,7973,7990,8007,8024,8041,8058,8075,8092,8109,8126,8143,8160,8177,8194,8211,8228,8245,8262,8279,8296,8313,8330,8347,8364,8381,8398,8415,8432,8449,8466,8483,8500,8517,8534,8551,8568,8585,8602,8619,8636,8653,8670,8687,8704,8721,8738,8755,8772,8789,8806,8823,8840,8857,8874,8891,8908,8925,8942,8959,8976,8993,9010,9027,9044,9061,9078,9095,9112,9129,9146,9163,9180,9197,9214,9231,9248,9265,9282,9299,9316,9333,9350,9367,9384,9401,9418,9435,9452,9469,9486,9503,9520,9537,9554,9571,9588,9605,9622,9639,9656,9673,9690,9707,9724,9741,9758,9775,9792,9809,9826,9843,9860,9877,9894,9911,9928,9945,9962,9979,9996,10013,10030,10047,10064,10081,10098,10115,10132,10149,10166,10183,10200,10217,10234,10251,10268,10285,10302,10319,10336,10353,10370,10387,10404,10421,10438,10455,10472,10489,10506,10523,10540,10557,10574,10591,10608,10625,10642,10659,10676,10693,10710,10727,10744,10761,10778,10795,10812,10829,10846,10863,10880,10897,10914,10931,10948,10965,10982,10999,11016,11033,11050,11067,11084,11101,11118,11135,11152,11169,11186,11203,11220,11237,11254,11271,11288,11305,11322,11339,11356,11373,11390,11407,11424,11441,11458,11475,11492,11509,11526,11543,11560,11577,11594,11611,11628,11645,11662,11679,11696,11713,11730,11747,11764,11781,11798,11815,11832,11849,11866,11883,11900,11917,11934,11951,11968,11985,12002,12019,12036,12053,12070,12087,12104,12121,12138,12155,12172,12189,12206,12223,12240,12257,12274,12291,12308,12325,12342,12359,12376,12393,12410,12427,12444,12461,12478,12495,12512,12529,12546,12563,12580,12597,12614,12631,12648,12665,12682,12699,12716,12733,12750,12767,12784,12801,12818,12835,12852,12869,12886,12903,12920,12937,12954,12971,12988,13005,13022,13039,13056,13073,13090,13107,13124,13141,13158,13175,13192,13209,13226,13243,13260,13277,13294,13311,13328,13345,13362,13379,13396,13413,13430,13447,13464,13481,13498,13515,13532,13549,13566,13583,13600,13617,13634,13651,13668,13685,13702,13719,13736,13753,13770,13787,13804,13821,13838,13855,13872,13889,13906,13923,13940,13957,13974,13991,14008,14025,14042,14059,14076,14093,14110,14127,14144,14161,14178,14195,14212,14229,14246,14263,14280,14297,14314,14331,14348,14365,14382,14399,14416,14433,14450,14467,14484,14501,14518,14535,14552,14569,14586,14603,14620,14637,14654,14671,14688,14705,14722,14739,14756,14773,14790,14807,14824,14841,14858,14875,14892,14909,14926,14943,14960,14977,14994,15011,15028,15045,15062,15079,15096,15113,15130,15147,15164,15181,15198,15215,15232,15249,15266,15283,15300,15317,15334,15351,15368,15385,15402,15419,15436,15453,15470,15487,15504,15521,15538,15555,15572,15589,15606,15623,15640,15657,15674,15691,15708,15725,15742,15759,15776,15793,15810,15827,15844,15861,15878,15895,1591
```
### before: new plan (15 ms)
```
-> Filter: (YSE_App_transient.id in (17,34,51,68,85,102,119,136,153,170,187,204,221,238,255,272,289,306,323,340,357,374,391,408,425,442,459,476,493,510,527,544,561,578,595,612,629,646,663,680,697,714,731,748,765,782,799,816,833,850,867,884,901,918,935,952,969,986,1003,1020,1037,1054,1071,1088,1105,1122,1139,1156,1173,1190,1207,1224,1241,1258,1275,1292,1309,1326,1343,1360,1377,1394,1411,1428,1445,1462,1479,1496,1513,1530,1547,1564,1581,1598,1615,1632,1649,1666,1683,1700,1717,1734,1751,1768,1785,1802,1819,1836,1853,1870,1887,1904,1921,1938,1955,1972,1989,2006,2023,2040,2057,2074,2091,2108,2125,2142,2159,2176,2193,2210,2227,2244,2261,2278,2295,2312,2329,2346,2363,2380,2397,2414,2431,2448,2465,2482,2499,2516,2533,2550,2567,2584,2601,2618,2635,2652,2669,2686,2703,2720,2737,2754,2771,2788,2805,2822,2839,2856,2873,2890,2907,2924,2941,2958,2975,2992,3009,3026,3043,3060,3077,3094,3111,3128,3145,3162,3179,3196,3213,3230,3247,3264,3281,3298,3315,3332,3349,3366,3383,3400,3417,3434,3451,3468,3485,3502,3519,3536,3553,3570,3587,3604,3621,3638,3655,3672,3689,3706,3723,3740,3757,3774,3791,3808,3825,3842,3859,3876,3893,3910,3927,3944,3961,3978,3995,4012,4029,4046,4063,4080,4097,4114,4131,4148,4165,4182,4199,4216,4233,4250,4267,4284,4301,4318,4335,4352,4369,4386,4403,4420,4437,4454,4471,4488,4505,4522,4539,4556,4573,4590,4607,4624,4641,4658,4675,4692,4709,4726,4743,4760,4777,4794,4811,4828,4845,4862,4879,4896,4913,4930,4947,4964,4981,4998,5015,5032,5049,5066,5083,5100,5117,5134,5151,5168,5185,5202,5219,5236,5253,5270,5287,5304,5321,5338,5355,5372,5389,5406,5423,5440,5457,5474,5491,5508,5525,5542,5559,5576,5593,5610,5627,5644,5661,5678,5695,5712,5729,5746,5763,5780,5797,5814,5831,5848,5865,5882,5899,5916,5933,5950,5967,5984,6001,6018,6035,6052,6069,6086,6103,6120,6137,6154,6171,6188,6205,6222,6239,6256,6273,6290,6307,6324,6341,6358,6375,6392,6409,6426,6443,6460,6477,6494,6511,6528,6545,6562,6579,6596,6613,6630,6647,6664,6681,6698,6715,6732,6749,6766,6783,6800,6817,6834,6851,6868,6885,6902,6919,6936,6953,6970,6987,7004,7021,7038,7055,7072,7089,7106,7123,7140,7157,7174,7191,7208,7225,7242,7259,7276,7293,7310,7327,7344,7361,7378,7395,7412,7429,7446,7463,7480,7497,7514,7531,7548,7565,7582,7599,7616,7633,7650,7667,7684,7701,7718,7735,7752,7769,7786,7803,7820,7837,7854,7871,7888,7905,7922,7939,7956,7973,7990,8007,8024,8041,8058,8075,8092,8109,8126,8143,8160,8177,8194,8211,8228,8245,8262,8279,8296,8313,8330,8347,8364,8381,8398,8415,8432,8449,8466,8483,8500,8517,8534,8551,8568,8585,8602,8619,8636,8653,8670,8687,8704,8721,8738,8755,8772,8789,8806,8823,8840,8857,8874,8891,8908,8925,8942,8959,8976,8993,9010,9027,9044,9061,9078,9095,9112,9129,9146,9163,9180,9197,9214,9231,9248,9265,9282,9299,9316,9333,9350,9367,9384,9401,9418,9435,9452,9469,9486,9503,9520,9537,9554,9571,9588,9605,9622,9639,9656,9673,9690,9707,9724,9741,9758,9775,9792,9809,9826,9843,9860,9877,9894,9911,9928,9945,9962,9979,9996,10013,10030,10047,10064,10081,10098,10115,10132,10149,10166,10183,10200,10217,10234,10251,10268,10285,10302,10319,10336,10353,10370,10387,10404,10421,10438,10455,10472,10489,10506,10523,10540,10557,10574,10591,10608,10625,10642,10659,10676,10693,10710,10727,10744,10761,10778,10795,10812,10829,10846,10863,10880,10897,10914,10931,10948,10965,10982,10999,11016,11033,11050,11067,11084,11101,11118,11135,11152,11169,11186,11203,11220,11237,11254,11271,11288,11305,11322,11339,11356,11373,11390,11407,11424,11441,11458,11475,11492,11509,11526,11543,11560,11577,11594,11611,11628,11645,11662,11679,11696,11713,11730,11747,11764,11781,11798,11815,11832,11849,11866,11883,11900,11917,11934,11951,11968,11985,12002,12019,12036,12053,12070,12087,12104,12121,12138,12155,12172,12189,12206,12223,12240,12257,12274,12291,12308,12325,12342,12359,12376,12393,12410,12427,12444,12461,12478,12495,12512,12529,12546,12563,12580,12597,12614,12631,12648,12665,12682,12699,12716,12733,12750,12767,12784,12801,12818,12835,12852,12869,12886,12903,12920,12937,12954,12971,12988,13005,13022,13039,13056,13073,13090,13107,13124,13141,13158,13175,13192,13209,13226,13243,13260,13277,13294,13311,13328,13345,13362,13379,13396,13413,13430,13447,13464,13481,13498,13515,13532,13549,13566,13583,13600,13617,13634,13651,13668,13685,13702,13719,13736,13753,13770,13787,13804,13821,13838,13855,13872,13889,13906,13923,13940,13957,13974,13991,14008,14025,14042,14059,14076,14093,14110,14127,14144,14161,14178,14195,14212,14229,14246,14263,14280,14297,14314,14331,14348,14365,14382,14399,14416,14433,14450,14467,14484,14501,14518,14535,14552,14569,14586,14603,14620,14637,14654,14671,14688,14705,14722,14739,14756,14773,14790,14807,14824,14841,14858,14875,14892,14909,14926,14943,14960,14977,14994,15011,15028,15045,15062,15079,15096,15113,15130,15147,15164,15181,15198,15215,15232,15249,15266,15283,15300,15317,15334,15351,15368,15385,15402,15419,15436,15453,15470,15487,15504,15521,15538,15555,15572,15589,15606,15623,15640,15657,15674,15691,15708,15725,15742,15759,15776,15793,15810,15827,15844,15861,15878,15895,1591
```

### after: old plan (22 ms)
```
-> Filter: (YSE_App_transient.id in (17,34,51,68,85,102,119,136,153,170,187,204,221,238,255,272,289,306,323,340,357,374,391,408,425,442,459,476,493,510,527,544,561,578,595,612,629,646,663,680,697,714,731,748,765,782,799,816,833,850,867,884,901,918,935,952,969,986,1003,1020,1037,1054,1071,1088,1105,1122,1139,1156,1173,1190,1207,1224,1241,1258,1275,1292,1309,1326,1343,1360,1377,1394,1411,1428,1445,1462,1479,1496,1513,1530,1547,1564,1581,1598,1615,1632,1649,1666,1683,1700,1717,1734,1751,1768,1785,1802,1819,1836,1853,1870,1887,1904,1921,1938,1955,1972,1989,2006,2023,2040,2057,2074,2091,2108,2125,2142,2159,2176,2193,2210,2227,2244,2261,2278,2295,2312,2329,2346,2363,2380,2397,2414,2431,2448,2465,2482,2499,2516,2533,2550,2567,2584,2601,2618,2635,2652,2669,2686,2703,2720,2737,2754,2771,2788,2805,2822,2839,2856,2873,2890,2907,2924,2941,2958,2975,2992,3009,3026,3043,3060,3077,3094,3111,3128,3145,3162,3179,3196,3213,3230,3247,3264,3281,3298,3315,3332,3349,3366,3383,3400,3417,3434,3451,3468,3485,3502,3519,3536,3553,3570,3587,3604,3621,3638,3655,3672,3689,3706,3723,3740,3757,3774,3791,3808,3825,3842,3859,3876,3893,3910,3927,3944,3961,3978,3995,4012,4029,4046,4063,4080,4097,4114,4131,4148,4165,4182,4199,4216,4233,4250,4267,4284,4301,4318,4335,4352,4369,4386,4403,4420,4437,4454,4471,4488,4505,4522,4539,4556,4573,4590,4607,4624,4641,4658,4675,4692,4709,4726,4743,4760,4777,4794,4811,4828,4845,4862,4879,4896,4913,4930,4947,4964,4981,4998,5015,5032,5049,5066,5083,5100,5117,5134,5151,5168,5185,5202,5219,5236,5253,5270,5287,5304,5321,5338,5355,5372,5389,5406,5423,5440,5457,5474,5491,5508,5525,5542,5559,5576,5593,5610,5627,5644,5661,5678,5695,5712,5729,5746,5763,5780,5797,5814,5831,5848,5865,5882,5899,5916,5933,5950,5967,5984,6001,6018,6035,6052,6069,6086,6103,6120,6137,6154,6171,6188,6205,6222,6239,6256,6273,6290,6307,6324,6341,6358,6375,6392,6409,6426,6443,6460,6477,6494,6511,6528,6545,6562,6579,6596,6613,6630,6647,6664,6681,6698,6715,6732,6749,6766,6783,6800,6817,6834,6851,6868,6885,6902,6919,6936,6953,6970,6987,7004,7021,7038,7055,7072,7089,7106,7123,7140,7157,7174,7191,7208,7225,7242,7259,7276,7293,7310,7327,7344,7361,7378,7395,7412,7429,7446,7463,7480,7497,7514,7531,7548,7565,7582,7599,7616,7633,7650,7667,7684,7701,7718,7735,7752,7769,7786,7803,7820,7837,7854,7871,7888,7905,7922,7939,7956,7973,7990,8007,8024,8041,8058,8075,8092,8109,8126,8143,8160,8177,8194,8211,8228,8245,8262,8279,8296,8313,8330,8347,8364,8381,8398,8415,8432,8449,8466,8483,8500,8517,8534,8551,8568,8585,8602,8619,8636,8653,8670,8687,8704,8721,8738,8755,8772,8789,8806,8823,8840,8857,8874,8891,8908,8925,8942,8959,8976,8993,9010,9027,9044,9061,9078,9095,9112,9129,9146,9163,9180,9197,9214,9231,9248,9265,9282,9299,9316,9333,9350,9367,9384,9401,9418,9435,9452,9469,9486,9503,9520,9537,9554,9571,9588,9605,9622,9639,9656,9673,9690,9707,9724,9741,9758,9775,9792,9809,9826,9843,9860,9877,9894,9911,9928,9945,9962,9979,9996,10013,10030,10047,10064,10081,10098,10115,10132,10149,10166,10183,10200,10217,10234,10251,10268,10285,10302,10319,10336,10353,10370,10387,10404,10421,10438,10455,10472,10489,10506,10523,10540,10557,10574,10591,10608,10625,10642,10659,10676,10693,10710,10727,10744,10761,10778,10795,10812,10829,10846,10863,10880,10897,10914,10931,10948,10965,10982,10999,11016,11033,11050,11067,11084,11101,11118,11135,11152,11169,11186,11203,11220,11237,11254,11271,11288,11305,11322,11339,11356,11373,11390,11407,11424,11441,11458,11475,11492,11509,11526,11543,11560,11577,11594,11611,11628,11645,11662,11679,11696,11713,11730,11747,11764,11781,11798,11815,11832,11849,11866,11883,11900,11917,11934,11951,11968,11985,12002,12019,12036,12053,12070,12087,12104,12121,12138,12155,12172,12189,12206,12223,12240,12257,12274,12291,12308,12325,12342,12359,12376,12393,12410,12427,12444,12461,12478,12495,12512,12529,12546,12563,12580,12597,12614,12631,12648,12665,12682,12699,12716,12733,12750,12767,12784,12801,12818,12835,12852,12869,12886,12903,12920,12937,12954,12971,12988,13005,13022,13039,13056,13073,13090,13107,13124,13141,13158,13175,13192,13209,13226,13243,13260,13277,13294,13311,13328,13345,13362,13379,13396,13413,13430,13447,13464,13481,13498,13515,13532,13549,13566,13583,13600,13617,13634,13651,13668,13685,13702,13719,13736,13753,13770,13787,13804,13821,13838,13855,13872,13889,13906,13923,13940,13957,13974,13991,14008,14025,14042,14059,14076,14093,14110,14127,14144,14161,14178,14195,14212,14229,14246,14263,14280,14297,14314,14331,14348,14365,14382,14399,14416,14433,14450,14467,14484,14501,14518,14535,14552,14569,14586,14603,14620,14637,14654,14671,14688,14705,14722,14739,14756,14773,14790,14807,14824,14841,14858,14875,14892,14909,14926,14943,14960,14977,14994,15011,15028,15045,15062,15079,15096,15113,15130,15147,15164,15181,15198,15215,15232,15249,15266,15283,15300,15317,15334,15351,15368,15385,15402,15419,15436,15453,15470,15487,15504,15521,15538,15555,15572,15589,15606,15623,15640,15657,15674,15691,15708,15725,15742,15759,15776,15793,15810,15827,15844,15861,15878,15895,1591
```
### after: new plan (18 ms)
```
-> Filter: (YSE_App_transient.id in (17,34,51,68,85,102,119,136,153,170,187,204,221,238,255,272,289,306,323,340,357,374,391,408,425,442,459,476,493,510,527,544,561,578,595,612,629,646,663,680,697,714,731,748,765,782,799,816,833,850,867,884,901,918,935,952,969,986,1003,1020,1037,1054,1071,1088,1105,1122,1139,1156,1173,1190,1207,1224,1241,1258,1275,1292,1309,1326,1343,1360,1377,1394,1411,1428,1445,1462,1479,1496,1513,1530,1547,1564,1581,1598,1615,1632,1649,1666,1683,1700,1717,1734,1751,1768,1785,1802,1819,1836,1853,1870,1887,1904,1921,1938,1955,1972,1989,2006,2023,2040,2057,2074,2091,2108,2125,2142,2159,2176,2193,2210,2227,2244,2261,2278,2295,2312,2329,2346,2363,2380,2397,2414,2431,2448,2465,2482,2499,2516,2533,2550,2567,2584,2601,2618,2635,2652,2669,2686,2703,2720,2737,2754,2771,2788,2805,2822,2839,2856,2873,2890,2907,2924,2941,2958,2975,2992,3009,3026,3043,3060,3077,3094,3111,3128,3145,3162,3179,3196,3213,3230,3247,3264,3281,3298,3315,3332,3349,3366,3383,3400,3417,3434,3451,3468,3485,3502,3519,3536,3553,3570,3587,3604,3621,3638,3655,3672,3689,3706,3723,3740,3757,3774,3791,3808,3825,3842,3859,3876,3893,3910,3927,3944,3961,3978,3995,4012,4029,4046,4063,4080,4097,4114,4131,4148,4165,4182,4199,4216,4233,4250,4267,4284,4301,4318,4335,4352,4369,4386,4403,4420,4437,4454,4471,4488,4505,4522,4539,4556,4573,4590,4607,4624,4641,4658,4675,4692,4709,4726,4743,4760,4777,4794,4811,4828,4845,4862,4879,4896,4913,4930,4947,4964,4981,4998,5015,5032,5049,5066,5083,5100,5117,5134,5151,5168,5185,5202,5219,5236,5253,5270,5287,5304,5321,5338,5355,5372,5389,5406,5423,5440,5457,5474,5491,5508,5525,5542,5559,5576,5593,5610,5627,5644,5661,5678,5695,5712,5729,5746,5763,5780,5797,5814,5831,5848,5865,5882,5899,5916,5933,5950,5967,5984,6001,6018,6035,6052,6069,6086,6103,6120,6137,6154,6171,6188,6205,6222,6239,6256,6273,6290,6307,6324,6341,6358,6375,6392,6409,6426,6443,6460,6477,6494,6511,6528,6545,6562,6579,6596,6613,6630,6647,6664,6681,6698,6715,6732,6749,6766,6783,6800,6817,6834,6851,6868,6885,6902,6919,6936,6953,6970,6987,7004,7021,7038,7055,7072,7089,7106,7123,7140,7157,7174,7191,7208,7225,7242,7259,7276,7293,7310,7327,7344,7361,7378,7395,7412,7429,7446,7463,7480,7497,7514,7531,7548,7565,7582,7599,7616,7633,7650,7667,7684,7701,7718,7735,7752,7769,7786,7803,7820,7837,7854,7871,7888,7905,7922,7939,7956,7973,7990,8007,8024,8041,8058,8075,8092,8109,8126,8143,8160,8177,8194,8211,8228,8245,8262,8279,8296,8313,8330,8347,8364,8381,8398,8415,8432,8449,8466,8483,8500,8517,8534,8551,8568,8585,8602,8619,8636,8653,8670,8687,8704,8721,8738,8755,8772,8789,8806,8823,8840,8857,8874,8891,8908,8925,8942,8959,8976,8993,9010,9027,9044,9061,9078,9095,9112,9129,9146,9163,9180,9197,9214,9231,9248,9265,9282,9299,9316,9333,9350,9367,9384,9401,9418,9435,9452,9469,9486,9503,9520,9537,9554,9571,9588,9605,9622,9639,9656,9673,9690,9707,9724,9741,9758,9775,9792,9809,9826,9843,9860,9877,9894,9911,9928,9945,9962,9979,9996,10013,10030,10047,10064,10081,10098,10115,10132,10149,10166,10183,10200,10217,10234,10251,10268,10285,10302,10319,10336,10353,10370,10387,10404,10421,10438,10455,10472,10489,10506,10523,10540,10557,10574,10591,10608,10625,10642,10659,10676,10693,10710,10727,10744,10761,10778,10795,10812,10829,10846,10863,10880,10897,10914,10931,10948,10965,10982,10999,11016,11033,11050,11067,11084,11101,11118,11135,11152,11169,11186,11203,11220,11237,11254,11271,11288,11305,11322,11339,11356,11373,11390,11407,11424,11441,11458,11475,11492,11509,11526,11543,11560,11577,11594,11611,11628,11645,11662,11679,11696,11713,11730,11747,11764,11781,11798,11815,11832,11849,11866,11883,11900,11917,11934,11951,11968,11985,12002,12019,12036,12053,12070,12087,12104,12121,12138,12155,12172,12189,12206,12223,12240,12257,12274,12291,12308,12325,12342,12359,12376,12393,12410,12427,12444,12461,12478,12495,12512,12529,12546,12563,12580,12597,12614,12631,12648,12665,12682,12699,12716,12733,12750,12767,12784,12801,12818,12835,12852,12869,12886,12903,12920,12937,12954,12971,12988,13005,13022,13039,13056,13073,13090,13107,13124,13141,13158,13175,13192,13209,13226,13243,13260,13277,13294,13311,13328,13345,13362,13379,13396,13413,13430,13447,13464,13481,13498,13515,13532,13549,13566,13583,13600,13617,13634,13651,13668,13685,13702,13719,13736,13753,13770,13787,13804,13821,13838,13855,13872,13889,13906,13923,13940,13957,13974,13991,14008,14025,14042,14059,14076,14093,14110,14127,14144,14161,14178,14195,14212,14229,14246,14263,14280,14297,14314,14331,14348,14365,14382,14399,14416,14433,14450,14467,14484,14501,14518,14535,14552,14569,14586,14603,14620,14637,14654,14671,14688,14705,14722,14739,14756,14773,14790,14807,14824,14841,14858,14875,14892,14909,14926,14943,14960,14977,14994,15011,15028,15045,15062,15079,15096,15113,15130,15147,15164,15181,15198,15215,15232,15249,15266,15283,15300,15317,15334,15351,15368,15385,15402,15419,15436,15453,15470,15487,15504,15521,15538,15555,15572,15589,15606,15623,15640,15657,15674,15691,15708,15725,15742,15759,15776,15793,15810,15827,15844,15861,15878,15895,1591
```

## code rising-transient last_mag_query x10 annotations (yse_python_queries), 400 rows

### before: old plan (442 ms)
```
-> Filter: (YSE_App_transient.id in (17,34,51,68,85,102,119,136,153,170,187,204,221,238,255,272,289,306,323,340,357,374,391,408,425,442,459,476,493,510,527,544,561,578,595,612,629,646,663,680,697,714,731,748,765,782,799,816,833,850,867,884,901,918,935,952,969,986,1003,1020,1037,1054,1071,1088,1105,1122,1139,1156,1173,1190,1207,1224,1241,1258,1275,1292,1309,1326,1343,1360,1377,1394,1411,1428,1445,1462,1479,1496,1513,1530,1547,1564,1581,1598,1615,1632,1649,1666,1683,1700,1717,1734,1751,1768,1785,1802,1819,1836,1853,1870,1887,1904,1921,1938,1955,1972,1989,2006,2023,2040,2057,2074,2091,2108,2125,2142,2159,2176,2193,2210,2227,2244,2261,2278,2295,2312,2329,2346,2363,2380,2397,2414,2431,2448,2465,2482,2499,2516,2533,2550,2567,2584,2601,2618,2635,2652,2669,2686,2703,2720,2737,2754,2771,2788,2805,2822,2839,2856,2873,2890,2907,2924,2941,2958,2975,2992,3009,3026,3043,3060,3077,3094,3111,3128,3145,3162,3179,3196,3213,3230,3247,3264,3281,3298,3315,3332,3349,3366,3383,3400,3417,3434,3451,3468,3485,3502,3519,3536,3553,3570,3587,3604,3621,3638,3655,3672,3689,3706,3723,3740,3757,3774,3791,3808,3825,3842,3859,3876,3893,3910,3927,3944,3961,3978,3995,4012,4029,4046,4063,4080,4097,4114,4131,4148,4165,4182,4199,4216,4233,4250,4267,4284,4301,4318,4335,4352,4369,4386,4403,4420,4437,4454,4471,4488,4505,4522,4539,4556,4573,4590,4607,4624,4641,4658,4675,4692,4709,4726,4743,4760,4777,4794,4811,4828,4845,4862,4879,4896,4913,4930,4947,4964,4981,4998,5015,5032,5049,5066,5083,5100,5117,5134,5151,5168,5185,5202,5219,5236,5253,5270,5287,5304,5321,5338,5355,5372,5389,5406,5423,5440,5457,5474,5491,5508,5525,5542,5559,5576,5593,5610,5627,5644,5661,5678,5695,5712,5729,5746,5763,5780,5797,5814,5831,5848,5865,5882,5899,5916,5933,5950,5967,5984,6001,6018,6035,6052,6069,6086,6103,6120,6137,6154,6171,6188,6205,6222,6239,6256,6273,6290,6307,6324,6341,6358,6375,6392,6409,6426,6443,6460,6477,6494,6511,6528,6545,6562,6579,6596,6613,6630,6647,6664,6681,6698,6715,6732,6749,6766,6783,6800))  (cost=182.17 rows=400) (actual time=0.029..1.635 rows=400 loops=1)
    -> Index range scan on YSE_App_transient using PRIMARY  (cost=182.17 rows=400) (actual time=0.027..1.343 rows=400 loops=1)
-> Select #2 (subquery in projection; dependent)
    -> Nested loop inner join  (cost=2.58 rows=1) (actual time=0.211..0.212 rows=1 loops=400)
        -> Nested loop inner join  (cost=1.85 rows=1) (actual time=0.208..0.209 rows=1 loops=400)
            -> Nested loop inner join  (cost=1.05 rows=1) (actual time=0.005..0.005 rows=1 loops=400)
                -> Nested loop inner join  (cost=0.70 rows=1) (actual time=0.002..0.003 rows=1 loops=400)
                    -> Index scan on pb using YSE_App_photometricb_instrument_id_31868fcf_fk_YSE_App_i  (cost=0.35 rows=1) (actual time=0.001..0.001 rows=1 loops=400)
                    -> Single-row index lookup on i using PRIMARY (id=pb.instrument_id)  (cost=0.35 rows=1) (actual time=0.001..0.001 rows=1 loops=400)
                -> Single-row index lookup on t using PRIMARY (id=YSE_App_transient.id)  (cost=0.35 rows=1) (actual time=0.002..0.002 rows=1 loops=400)
            -> Filter: ((pd.band_id = pb.id) and (pd.id = (select #3)))  (cost=0.80 rows=1) (actual time=0.203..0.203 rows=1 loops=400)
                -> Single-row index lookup on pd using PRIMARY (id=(select #3))  (cost=0.80 rows=1) (actual time=0.161..0.161 rows=1 loops=400)
                -> Select #3 (subquery in condition; dependent)
                    -> Limit: 1 row(s)  (actual time=0.066..0.066 rows=1 loops=1200)
                        -> Sort: pd2.obs_date DESC, limit input to 1 row(s) per chunk  (actual time=0.066..0.066 rows=1 loops=1200)
                            -> Stream results  (cost=19.19 rows=23) (actual time=0.032..0.063 rows=35 loops=1200)
                                -> Nested loop inner join  (cost=19.19 rows=23) (actual time=0.031..0.058 rows=35 loops=1200)
                                    -> Nested loop inner join  (cost=1.08 rows=1) (actual time=0.004..0.006 rows=2 loops=1200)
                                        -> Nested loop inner join  (cost=0.70 rows=1) (actual time=0.003..0.003 rows=1 loops=1200)
                                            -> Filter: (pb2.`name` in ('r-ZTF','r','rp','r-Sloan'))  (cost=0.35 rows=1) (actual time=0.001..0.002 rows=1 loops=1200)
                                                -> Table scan on pb2  (cost=0.35 rows=1) (actual time=0.001..0.001 rows=1 loops=1200)
                                            -> Filter: (i2.`name` <> 'Gaia-Photometric')  (cost=0.35 rows=1) (actual time=0.001..0.001 rows=1 loops=1200)
                                                -> Single-row index lookup on i2 using PRIMARY (id=pb2.instrument_id)  (cost=0.35 rows=1) (actual time=0.001..0.001 rows=1 loops=1200)
                                        -> Index lookup on p2 using YSE_App_transientpho_transient_id_9770bdf2_fk_YSE_App_t (transient_id=t.id)  (cost=0.38 rows=1) (actual time=0.001..0.002 rows=2 loops=1200)
     
```
### before: new plan (141 ms)
```
-> Filter: (YSE_App_transient.id in (17,34,51,68,85,102,119,136,153,170,187,204,221,238,255,272,289,306,323,340,357,374,391,408,425,442,459,476,493,510,527,544,561,578,595,612,629,646,663,680,697,714,731,748,765,782,799,816,833,850,867,884,901,918,935,952,969,986,1003,1020,1037,1054,1071,1088,1105,1122,1139,1156,1173,1190,1207,1224,1241,1258,1275,1292,1309,1326,1343,1360,1377,1394,1411,1428,1445,1462,1479,1496,1513,1530,1547,1564,1581,1598,1615,1632,1649,1666,1683,1700,1717,1734,1751,1768,1785,1802,1819,1836,1853,1870,1887,1904,1921,1938,1955,1972,1989,2006,2023,2040,2057,2074,2091,2108,2125,2142,2159,2176,2193,2210,2227,2244,2261,2278,2295,2312,2329,2346,2363,2380,2397,2414,2431,2448,2465,2482,2499,2516,2533,2550,2567,2584,2601,2618,2635,2652,2669,2686,2703,2720,2737,2754,2771,2788,2805,2822,2839,2856,2873,2890,2907,2924,2941,2958,2975,2992,3009,3026,3043,3060,3077,3094,3111,3128,3145,3162,3179,3196,3213,3230,3247,3264,3281,3298,3315,3332,3349,3366,3383,3400,3417,3434,3451,3468,3485,3502,3519,3536,3553,3570,3587,3604,3621,3638,3655,3672,3689,3706,3723,3740,3757,3774,3791,3808,3825,3842,3859,3876,3893,3910,3927,3944,3961,3978,3995,4012,4029,4046,4063,4080,4097,4114,4131,4148,4165,4182,4199,4216,4233,4250,4267,4284,4301,4318,4335,4352,4369,4386,4403,4420,4437,4454,4471,4488,4505,4522,4539,4556,4573,4590,4607,4624,4641,4658,4675,4692,4709,4726,4743,4760,4777,4794,4811,4828,4845,4862,4879,4896,4913,4930,4947,4964,4981,4998,5015,5032,5049,5066,5083,5100,5117,5134,5151,5168,5185,5202,5219,5236,5253,5270,5287,5304,5321,5338,5355,5372,5389,5406,5423,5440,5457,5474,5491,5508,5525,5542,5559,5576,5593,5610,5627,5644,5661,5678,5695,5712,5729,5746,5763,5780,5797,5814,5831,5848,5865,5882,5899,5916,5933,5950,5967,5984,6001,6018,6035,6052,6069,6086,6103,6120,6137,6154,6171,6188,6205,6222,6239,6256,6273,6290,6307,6324,6341,6358,6375,6392,6409,6426,6443,6460,6477,6494,6511,6528,6545,6562,6579,6596,6613,6630,6647,6664,6681,6698,6715,6732,6749,6766,6783,6800))  (cost=182.17 rows=400) (actual time=0.027..0.909 rows=400 loops=1)
    -> Index range scan on YSE_App_transient using PRIMARY  (cost=182.17 rows=400) (actual time=0.026..0.819 rows=400 loops=1)
-> Select #2 (subquery in projection; dependent)
    -> Limit: 1 row(s)  (actual time=0.089..0.089 rows=1 loops=400)
        -> Sort: pd.obs_date DESC, limit input to 1 row(s) per chunk  (actual time=0.089..0.089 rows=1 loops=400)
            -> Stream results  (cost=19.19 rows=23) (actual time=0.045..0.086 rows=35 loops=400)
                -> Nested loop inner join  (cost=19.19 rows=23) (actual time=0.045..0.082 rows=35 loops=400)
                    -> Nested loop inner join  (cost=1.08 rows=1) (actual time=0.004..0.005 rows=2 loops=400)
                        -> Nested loop inner join  (cost=0.70 rows=1) (actual time=0.002..0.003 rows=1 loops=400)
                            -> Filter: (pb.`name` in ('r-ZTF','r','rp','r-Sloan'))  (cost=0.35 rows=1) (actual time=0.001..0.001 rows=1 loops=400)
                                -> Table scan on pb  (cost=0.35 rows=1) (actual time=0.001..0.001 rows=1 loops=400)
                            -> Filter: (i.`name` <> 'Gaia-Photometric')  (cost=0.35 rows=1) (actual time=0.001..0.001 rows=1 loops=400)
                                -> Single-row index lookup on i using PRIMARY (id=pb.instrument_id)  (cost=0.35 rows=1) (actual time=0.001..0.001 rows=1 loops=400)
                        -> Index lookup on p using YSE_App_transientpho_transient_id_9770bdf2_fk_YSE_App_t (transient_id=YSE_App_transient.id)  (cost=0.38 rows=1) (actual time=0.002..0.002 rows=2 loops=400)
                    -> Filter: (pd.band_id = pb.id)  (cost=15.54 rows=20) (actual time=0.040..0.043 rows=20 loops=694)
                        -> Index lookup on pd using YSE_App_transientpho_photometry_id_fbc5dfd2_fk_YSE_App_t (photometry_id=p.id)  (cost=15.54 rows=20) (actual time=0.040..0.042 rows=20 loops=694)
-> Select #3 (subquery in projection; dependent)
    -> Limit: 1 row(s)  (actual time=0.039..0.039 rows=1 loops=400)
        -> Sort: pd.obs_date DESC, limit input to 1 row(s) per chunk  (actual time=0.039..0.039 rows=1 loops=400)
            -> Stream results  (cost=19.19 rows=23) (actual time=0.016..0.036 rows=35 loops=400)
                -> Nested loop inner join  (cost=19.19 rows=23) (actual time=0.016..0.032 rows=35 loops=400)
                    -> Nested loop inner join  (cost=1.08 rows=1) (actual time=0.004..0.005 rows=2 loops=400)
                        -> Nested loop inner join  (cost=0.70 rows=1) (actual time=0.002..0.003 rows=1 loops=400)
                            -> Filter: (pb.`name` in ('r-ZTF','r','rp','r-Sloan'))  (cost=0.35 rows=1) (actual time=0.001..0.002 rows=1 loops=400)
                                -> Table scan on pb  (cost=0.35 rows=1) (actual time=0.001..0.001 rows=1 loops=400)
                            -> Filter: (i.`name` <> 'Gaia-Photometric')  (cost=0.35 rows=1) (actual time=0.001..0.001 rows=1 loops=400)
                      
```

### after: old plan (466 ms)
```
-> Filter: (YSE_App_transient.id in (17,34,51,68,85,102,119,136,153,170,187,204,221,238,255,272,289,306,323,340,357,374,391,408,425,442,459,476,493,510,527,544,561,578,595,612,629,646,663,680,697,714,731,748,765,782,799,816,833,850,867,884,901,918,935,952,969,986,1003,1020,1037,1054,1071,1088,1105,1122,1139,1156,1173,1190,1207,1224,1241,1258,1275,1292,1309,1326,1343,1360,1377,1394,1411,1428,1445,1462,1479,1496,1513,1530,1547,1564,1581,1598,1615,1632,1649,1666,1683,1700,1717,1734,1751,1768,1785,1802,1819,1836,1853,1870,1887,1904,1921,1938,1955,1972,1989,2006,2023,2040,2057,2074,2091,2108,2125,2142,2159,2176,2193,2210,2227,2244,2261,2278,2295,2312,2329,2346,2363,2380,2397,2414,2431,2448,2465,2482,2499,2516,2533,2550,2567,2584,2601,2618,2635,2652,2669,2686,2703,2720,2737,2754,2771,2788,2805,2822,2839,2856,2873,2890,2907,2924,2941,2958,2975,2992,3009,3026,3043,3060,3077,3094,3111,3128,3145,3162,3179,3196,3213,3230,3247,3264,3281,3298,3315,3332,3349,3366,3383,3400,3417,3434,3451,3468,3485,3502,3519,3536,3553,3570,3587,3604,3621,3638,3655,3672,3689,3706,3723,3740,3757,3774,3791,3808,3825,3842,3859,3876,3893,3910,3927,3944,3961,3978,3995,4012,4029,4046,4063,4080,4097,4114,4131,4148,4165,4182,4199,4216,4233,4250,4267,4284,4301,4318,4335,4352,4369,4386,4403,4420,4437,4454,4471,4488,4505,4522,4539,4556,4573,4590,4607,4624,4641,4658,4675,4692,4709,4726,4743,4760,4777,4794,4811,4828,4845,4862,4879,4896,4913,4930,4947,4964,4981,4998,5015,5032,5049,5066,5083,5100,5117,5134,5151,5168,5185,5202,5219,5236,5253,5270,5287,5304,5321,5338,5355,5372,5389,5406,5423,5440,5457,5474,5491,5508,5525,5542,5559,5576,5593,5610,5627,5644,5661,5678,5695,5712,5729,5746,5763,5780,5797,5814,5831,5848,5865,5882,5899,5916,5933,5950,5967,5984,6001,6018,6035,6052,6069,6086,6103,6120,6137,6154,6171,6188,6205,6222,6239,6256,6273,6290,6307,6324,6341,6358,6375,6392,6409,6426,6443,6460,6477,6494,6511,6528,6545,6562,6579,6596,6613,6630,6647,6664,6681,6698,6715,6732,6749,6766,6783,6800))  (cost=186.57 rows=400) (actual time=0.032..2.223 rows=400 loops=1)
    -> Index range scan on YSE_App_transient using PRIMARY  (cost=186.57 rows=400) (actual time=0.030..1.871 rows=400 loops=1)
-> Select #2 (subquery in projection; dependent)
    -> Nested loop inner join  (cost=2.08 rows=1) (actual time=0.210..0.211 rows=1 loops=400)
        -> Nested loop inner join  (cost=1.71 rows=1) (actual time=0.206..0.207 rows=1 loops=400)
            -> Nested loop inner join  (cost=1.06 rows=1) (actual time=0.005..0.006 rows=1 loops=400)
                -> Nested loop inner join  (cost=0.70 rows=1) (actual time=0.003..0.004 rows=1 loops=400)
                    -> Index scan on pb using YSE_App_photometricb_instrument_id_31868fcf_fk_YSE_App_i  (cost=0.35 rows=1) (actual time=0.001..0.002 rows=1 loops=400)
                    -> Single-row index lookup on i using PRIMARY (id=pb.instrument_id)  (cost=0.35 rows=1) (actual time=0.001..0.001 rows=1 loops=400)
                -> Single-row index lookup on t using PRIMARY (id=YSE_App_transient.id)  (cost=0.36 rows=1) (actual time=0.002..0.002 rows=1 loops=400)
            -> Filter: ((pd.band_id = pb.id) and (pd.id = (select #3)))  (cost=0.64 rows=1) (actual time=0.200..0.200 rows=1 loops=400)
                -> Single-row index lookup on pd using PRIMARY (id=(select #3))  (cost=0.64 rows=1) (actual time=0.156..0.156 rows=1 loops=400)
                -> Select #3 (subquery in condition; dependent)
                    -> Limit: 1 row(s)  (actual time=0.065..0.065 rows=1 loops=1200)
                        -> Sort: pd2.obs_date DESC, limit input to 1 row(s) per chunk  (actual time=0.065..0.065 rows=1 loops=1200)
                            -> Stream results  (cost=15.62 rows=23) (actual time=0.029..0.061 rows=35 loops=1200)
                                -> Nested loop inner join  (cost=15.62 rows=23) (actual time=0.029..0.056 rows=35 loops=1200)
                                    -> Nested loop inner join  (cost=1.06 rows=1) (actual time=0.005..0.006 rows=2 loops=1200)
                                        -> Nested loop inner join  (cost=0.70 rows=1) (actual time=0.003..0.004 rows=1 loops=1200)
                                            -> Filter: (pb2.`name` in ('r-ZTF','r','rp','r-Sloan'))  (cost=0.35 rows=1) (actual time=0.002..0.002 rows=1 loops=1200)
                                                -> Table scan on pb2  (cost=0.35 rows=1) (actual time=0.001..0.002 rows=1 loops=1200)
                                            -> Filter: (i2.`name` <> 'Gaia-Photometric')  (cost=0.35 rows=1) (actual time=0.001..0.001 rows=1 loops=1200)
                                                -> Single-row index lookup on i2 using PRIMARY (id=pb2.instrument_id)  (cost=0.35 rows=1) (actual time=0.001..0.001 rows=1 loops=1200)
                                        -> Index lookup on p2 using YSE_App_transientpho_transient_id_9770bdf2_fk_YSE_App_t (transient_id=t.id)  (cost=0.36 rows=1) (actual time=0.002..0.002 rows=2 loops=1200)
     
```
### after: new plan (208 ms)
```
-> Filter: (YSE_App_transient.id in (17,34,51,68,85,102,119,136,153,170,187,204,221,238,255,272,289,306,323,340,357,374,391,408,425,442,459,476,493,510,527,544,561,578,595,612,629,646,663,680,697,714,731,748,765,782,799,816,833,850,867,884,901,918,935,952,969,986,1003,1020,1037,1054,1071,1088,1105,1122,1139,1156,1173,1190,1207,1224,1241,1258,1275,1292,1309,1326,1343,1360,1377,1394,1411,1428,1445,1462,1479,1496,1513,1530,1547,1564,1581,1598,1615,1632,1649,1666,1683,1700,1717,1734,1751,1768,1785,1802,1819,1836,1853,1870,1887,1904,1921,1938,1955,1972,1989,2006,2023,2040,2057,2074,2091,2108,2125,2142,2159,2176,2193,2210,2227,2244,2261,2278,2295,2312,2329,2346,2363,2380,2397,2414,2431,2448,2465,2482,2499,2516,2533,2550,2567,2584,2601,2618,2635,2652,2669,2686,2703,2720,2737,2754,2771,2788,2805,2822,2839,2856,2873,2890,2907,2924,2941,2958,2975,2992,3009,3026,3043,3060,3077,3094,3111,3128,3145,3162,3179,3196,3213,3230,3247,3264,3281,3298,3315,3332,3349,3366,3383,3400,3417,3434,3451,3468,3485,3502,3519,3536,3553,3570,3587,3604,3621,3638,3655,3672,3689,3706,3723,3740,3757,3774,3791,3808,3825,3842,3859,3876,3893,3910,3927,3944,3961,3978,3995,4012,4029,4046,4063,4080,4097,4114,4131,4148,4165,4182,4199,4216,4233,4250,4267,4284,4301,4318,4335,4352,4369,4386,4403,4420,4437,4454,4471,4488,4505,4522,4539,4556,4573,4590,4607,4624,4641,4658,4675,4692,4709,4726,4743,4760,4777,4794,4811,4828,4845,4862,4879,4896,4913,4930,4947,4964,4981,4998,5015,5032,5049,5066,5083,5100,5117,5134,5151,5168,5185,5202,5219,5236,5253,5270,5287,5304,5321,5338,5355,5372,5389,5406,5423,5440,5457,5474,5491,5508,5525,5542,5559,5576,5593,5610,5627,5644,5661,5678,5695,5712,5729,5746,5763,5780,5797,5814,5831,5848,5865,5882,5899,5916,5933,5950,5967,5984,6001,6018,6035,6052,6069,6086,6103,6120,6137,6154,6171,6188,6205,6222,6239,6256,6273,6290,6307,6324,6341,6358,6375,6392,6409,6426,6443,6460,6477,6494,6511,6528,6545,6562,6579,6596,6613,6630,6647,6664,6681,6698,6715,6732,6749,6766,6783,6800))  (cost=186.57 rows=400) (actual time=0.031..1.655 rows=400 loops=1)
    -> Index range scan on YSE_App_transient using PRIMARY  (cost=186.57 rows=400) (actual time=0.029..1.475 rows=400 loops=1)
-> Select #2 (subquery in projection; dependent)
    -> Limit: 1 row(s)  (actual time=0.098..0.098 rows=1 loops=400)
        -> Sort: pd.obs_date DESC, limit input to 1 row(s) per chunk  (actual time=0.098..0.098 rows=1 loops=400)
            -> Stream results  (cost=15.62 rows=23) (actual time=0.049..0.093 rows=35 loops=400)
                -> Nested loop inner join  (cost=15.62 rows=23) (actual time=0.048..0.088 rows=35 loops=400)
                    -> Nested loop inner join  (cost=1.06 rows=1) (actual time=0.006..0.008 rows=2 loops=400)
                        -> Nested loop inner join  (cost=0.70 rows=1) (actual time=0.004..0.004 rows=1 loops=400)
                            -> Filter: (pb.`name` in ('r-ZTF','r','rp','r-Sloan'))  (cost=0.35 rows=1) (actual time=0.002..0.002 rows=1 loops=400)
                                -> Table scan on pb  (cost=0.35 rows=1) (actual time=0.002..0.002 rows=1 loops=400)
                            -> Filter: (i.`name` <> 'Gaia-Photometric')  (cost=0.35 rows=1) (actual time=0.002..0.002 rows=1 loops=400)
                                -> Single-row index lookup on i using PRIMARY (id=pb.instrument_id)  (cost=0.35 rows=1) (actual time=0.001..0.001 rows=1 loops=400)
                        -> Index lookup on p using YSE_App_transientpho_transient_id_9770bdf2_fk_YSE_App_t (transient_id=YSE_App_transient.id)  (cost=0.36 rows=1) (actual time=0.002..0.003 rows=2 loops=400)
                    -> Filter: (pd.band_id = pb.id)  (cost=12.44 rows=20) (actual time=0.041..0.045 rows=20 loops=694)
                        -> Index lookup on pd using YSE_App_transientpho_photometry_id_fbc5dfd2_fk_YSE_App_t (photometry_id=p.id)  (cost=12.44 rows=20) (actual time=0.040..0.043 rows=20 loops=694)
-> Select #3 (subquery in projection; dependent)
    -> Limit: 1 row(s)  (actual time=0.057..0.057 rows=1 loops=400)
        -> Sort: pd.obs_date DESC, limit input to 1 row(s) per chunk  (actual time=0.056..0.056 rows=1 loops=400)
            -> Stream results  (cost=15.62 rows=23) (actual time=0.022..0.052 rows=35 loops=400)
                -> Nested loop inner join  (cost=15.62 rows=23) (actual time=0.022..0.046 rows=35 loops=400)
                    -> Nested loop inner join  (cost=1.06 rows=1) (actual time=0.006..0.007 rows=2 loops=400)
                        -> Nested loop inner join  (cost=0.70 rows=1) (actual time=0.004..0.005 rows=1 loops=400)
                            -> Filter: (pb.`name` in ('r-ZTF','r','rp','r-Sloan'))  (cost=0.35 rows=1) (actual time=0.002..0.003 rows=1 loops=400)
                                -> Table scan on pb  (cost=0.35 rows=1) (actual time=0.002..0.002 rows=1 loops=400)
                            -> Filter: (i.`name` <> 'Gaia-Photometric')  (cost=0.35 rows=1) (actual time=0.002..0.002 rows=1 loops=400)
                      
```
