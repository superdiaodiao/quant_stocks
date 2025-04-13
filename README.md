# Historical Data Resource

get history data at https://stooq.com/db/h/

# Prepare

## add pub key in github

read pub key file

```shell
cat ~/.ssh/id_rsa.pub
```

paste it into `Setting-Security-Deploy keys` part

## prepare stock list

download list from https://www.nasdaq.com/market-activity/stocks/screener?page=1&rows_per_page=25

# Check

run `test_get_stocks.py`, make sure the IP is not blocked.
