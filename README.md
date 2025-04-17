# 0.Git

## add pub key in github

read pub key file

```shell
cat ~/.ssh/id_rsa.pub
```

paste it into `Setting-Security-Deploy keys` part

# 1.python env
## conda
```shell
# install miniconda
mkdir -p ~/miniconda3
wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh -O ~/miniconda3/miniconda.sh
bash ~/miniconda3/miniconda.sh -b -u -p ~/miniconda3
rm ~/miniconda3/miniconda.sh
source ~/miniconda3/bin/activate
conda init --all

# create env
conda create -n quant_stocks python=3.10
conda activate quant_stocks
```

## ta-lib
check https://github.com/TA-Lib/ta-lib-python
```shell
cd /data/
wget https://github.com/ta-lib/ta-lib/releases/download/v0.6.4/ta-lib-0.6.4-src.tar.gz
tar -xzf ta-lib-0.6.4-src.tar.gz
cd ta-lib-0.6.4/
./configure --prefix=/usr
make
sudo make install

python -m pip install TA-Lib
```

## packages
```shell
pip install -r requirements.txt
```

# 2.Historical Data Resource

get history data at https://stooq.com/db/h/

# 3.Prepare Stock List

download list from https://www.nasdaq.com/market-activity/stocks/screener?page=1&rows_per_page=25

# 4.Crontab
```shell
chmod 777 /data/quant_stocks/schedule_run.sh
```
```shell
crontab -e
```
```shell
0 9 * * * /data/quant_stocks/schedule_run.sh
```
