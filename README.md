# Historical Data Resource
get history data at https://stooq.com/db/h/

# Prepare

## add pub key in github
read pub key file
```shell
cat ~/.ssh/id_rsa.pub
```
paste it into `Setting-Security-Deploy keys` part

## prepare for mysql
```mysql
CREATE DATABASE quant_trading;
CREATE USER 'quant_user'@'%' IDENTIFIED BY 'secure_password';
GRANT ALL PRIVILEGES ON quant_trading.* TO 'quant_user'@'%';
FLUSH PRIVILEGES;
```

# Check
run `test_get_stocks.py`, make sure the IP is not blocked.
