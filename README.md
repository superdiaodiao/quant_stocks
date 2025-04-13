# python env
## conda
```shell
# install miniconda
mkdir -p ~/miniconda3
wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh -O ~/miniconda3/miniconda.sh
bash ~/miniconda3/miniconda.sh -b -u -p ~/miniconda3
rm ~/miniconda3/miniconda.sh

# create env
conda create -n quant_stocks python=3.10
conda activate quant_stocks
```

## ta-lib
check https://github.com/TA-Lib/ta-lib-python
```shell
conda install -c conda-forge ta-lib
```

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
