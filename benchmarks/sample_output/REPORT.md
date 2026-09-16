# RelPyDB Benchmark Report

*Generated 2026-09-15 20:21:59*

## Environment

- **timestamp**: 2026-09-15 20:21:59
- **python**: 3.12.3
- **implementation**: CPython
- **platform**: Linux-6.18.44-fc-v33-x86_64-with-glibc2.39
- **processor**: x86_64
- **cpu_count**: 1
- **relpy_version**: 2.0.0
- **libraries**: numpy 2.4.4, pandas 3.0.2, duckdb 1.5.5, sqlalchemy 2.0.53, sqlite3 3.45.1
- **total_ram_gb**: 4.2

## Full console report

```
==============================================================================
 RelPyDB — Comprehensive Benchmark
==============================================================================
 python 3.12.3 (CPython) on Linux-6.18.44-fc-v33-x86_64-with-glibc2.39
 cpu=1  relpy=2.0.0  engine=C (required)
 libraries: numpy 2.4.4, pandas 3.0.2, duckdb 1.5.5, sqlalchemy 2.0.53, sqlite3 3.45.1
 systems:   RelPy, pandas, sqlite3, DuckDB, SQLAlchemy, Python loops
 sizes:     1,000, 10,000   repeats=3 warmup=2

##############################################################################
# DATASET: 1,000 users + 1,000 orders
##############################################################################

Query time by system  (median ms; lower is better)
Workload                               RelPy   pandas  sqlite3   DuckDB  SQLAlchemy  Python loops            Fastest
-----------------------------------  -------  -------  -------  -------  ----------  ------------  -----------------
── Filters ───────────────                                                                                          
WHERE city = 'haifa'                    0.78     0.61     0.15     1.04        0.48          0.07  Python loops 0.07
WHERE age > 50                          1.37     0.57     0.22     0.83        0.61          0.10  Python loops 0.10
WHERE age BETWEEN 30 AND 40             0.83     0.61     0.14     0.68        0.45          0.08  Python loops 0.08
WHERE city='eilat' OR age<25            0.92     0.73     0.16     0.87        0.57          0.10  Python loops 0.10
WHERE city IN (3)                       1.17     0.57     0.26     0.83        0.71          0.10  Python loops 0.10
WHERE (30<=age<=60) OR city='haifa'     1.44     0.79     0.25     0.84        0.76          0.11  Python loops 0.11
── Aggregates ────────────                                                                                          
COUNT WHERE age>50                      0.09     0.24     0.07     0.64        0.49          0.06  Python loops 0.06
SUM(score) WHERE active                 0.09     0.43     0.09     0.75        0.45          0.07  Python loops 0.07
AVG(age)                                0.06     0.19     0.07     0.68        0.38          0.09         RelPy 0.06
MIN/MAX(score)                          0.08     0.27     0.10     0.66        0.45          0.10         RelPy 0.08
── Group by ──────────────                                                                                          
GROUP BY city -> count, avg             0.26     1.37     0.31     0.98        0.69          0.19  Python loops 0.19
GROUP BY city, active                   0.26     2.06     0.43     1.31        0.77          0.21  Python loops 0.21
── Joins ─────────────────                                                                                          
JOIN + WHERE amount>450                 1.24     1.15     0.14     1.32        0.50          0.09  Python loops 0.09
JOIN + GROUP BY city SUM                0.57     1.38     0.47     1.66        0.83          0.36  Python loops 0.36
── Order/Distinct ────────                                                                                          
ORDER BY score DESC LIMIT 200           1.29     0.69     0.38     0.81        0.69          0.30  Python loops 0.30
DISTINCT city                           1.26     0.25     0.13     0.92        0.37          0.06  Python loops 0.06
SELECT id,city WHERE age>60             1.11     1.35     0.31     0.70        0.66          0.11  Python loops 0.11

Correctness: OK   (102 cross-checks)

##############################################################################
# DATASET: 10,000 users + 10,000 orders
##############################################################################

Query time by system  (median ms; lower is better)
Workload                               RelPy   pandas  sqlite3   DuckDB  SQLAlchemy  Python loops            Fastest
-----------------------------------  -------  -------  -------  -------  ----------  ------------  -----------------
── Filters ───────────────                                                                                          
WHERE city = 'haifa'                    6.21     1.30     0.98     0.99        1.45          0.45  Python loops 0.45
WHERE age > 50                         12.30     0.89     1.92     1.49        2.84          0.57  Python loops 0.57
WHERE age BETWEEN 30 AND 40             6.78     0.65     0.81     0.86        1.35          0.54  Python loops 0.54
WHERE city='eilat' OR age<25            7.39     1.26     1.20     1.15        1.99          0.70  Python loops 0.70
WHERE city IN (3)                      10.46     1.02     1.90     1.74        3.03          0.60  Python loops 0.60
WHERE (30<=age<=60) OR city='haifa'    11.90     1.67     1.94     1.99        3.43          0.82  Python loops 0.82
── Aggregates ────────────                                                                                          
COUNT WHERE age>50                      0.23     0.25     0.43     0.77        0.79          0.51         RelPy 0.23
SUM(score) WHERE active                 0.21     0.47     0.56     0.95        0.87          0.35         RelPy 0.21
AVG(age)                                0.10     0.21     0.41     0.87        0.74          0.43         RelPy 0.10
MIN/MAX(score)                          0.15     0.26     0.70     1.02        1.02          0.77         RelPy 0.15
── Group by ──────────────                                                                                          
GROUP BY city -> count, avg             1.12     2.16     2.86     1.52        2.80          1.14         RelPy 1.12
GROUP BY city, active                   1.28     2.86     3.79     1.51        3.93          1.60         RelPy 1.28
── Joins ─────────────────                                                                                          
JOIN + WHERE amount>450                12.96     1.50     0.98     1.77        1.51          0.52  Python loops 0.52
JOIN + GROUP BY city SUM                4.71     2.78     4.75     2.37        5.13          3.11        DuckDB 2.37
── Order/Distinct ────────                                                                                          
ORDER BY score DESC LIMIT 200           8.95     1.11     1.02     0.94        1.23          2.14        DuckDB 0.94
DISTINCT city                          12.28     0.59     0.86     1.37        1.17          0.45  Python loops 0.45
SELECT id,city WHERE age>60             9.51     3.57     2.36     2.24        3.45          0.93  Python loops 0.93

Correctness: OK   (102 cross-checks)

##############################################################################
# INGESTION throughput
##############################################################################
Ingestion (rows/sec)   RelPy   pandas  sqlite3  DuckDB  SQLAlchemy  Python loops
--------------------  ------  -------  -------  ------  ----------  ------------
1,000 rows            68,016  465,875  497,298   5,079     130,190    13,428,226
10,000 rows           65,533  778,365  623,945   5,157     202,454    17,880,291

##############################################################################
# RelPy CAPABILITIES (native engine on)
##############################################################################
RelPy capability (median ms)    1,000    10,000
----------------------------  -------  --------
update_bulk (active)           316.91  29055.08
delete_bulk (amount<50)         13.04    127.37
to_list (all users)              1.87     16.63
to_pandas                        2.99     21.80
to_json                          5.76     54.59
save + load                     22.31    221.23
query on encrypted db           17.38    168.95
query on plaintext db            0.10      0.23
encryption overhead (x)        166.0x    732.4x
(capability timings measured at sizes: 1,000, 10,000)

##############################################################################
# SUMMARY
##############################################################################
Fastest-on-workload wins  count  share
------------------------  -----  -----
Python loops                 24  70.6%
RelPy                         8  23.5%
DuckDB                        2   5.9%

Correctness overall: ALL CONSISTENT

```
