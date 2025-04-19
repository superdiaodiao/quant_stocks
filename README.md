# 0.Git

## add pub key in github

read pub key file

```shell
cat ~/.ssh/id_rsa.pub
```

paste it into `Setting-Security-Deploy keys` part

## clone git repo
```shell
cd /data/
git clone xxx.git
```


# 1.python env
## conda
```shell
# install miniconda
cd /data/
mkdir -p /data/miniconda3
wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh -O /data/miniconda3/miniconda.sh
bash /data/miniconda3/miniconda.sh -b -u -p /data/miniconda3
rm /data/miniconda3/miniconda.sh
source /data/miniconda3/bin/activate
conda init --all

# create env
conda create -n quant_stocks python=3.10
conda activate quant_stocks
```


## set up python project path
find current site-packages pyth by:
```shell
python -m site
```
you will find a path like:

```shell
/data/miniconda3/envs/quant_stocks/lib/python3.10/site-packages
```
creath a .pth file and echo:
```shell
echo "/data/quant_stocks" > /data/miniconda3/envs/quant_stocks/lib/python3.10/site-packages/quant_stocks.pth
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
