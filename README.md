# System environment
Ryu Server: Ubuntu22.04<br>
Ryu Python: Python3.9<br>
Snort Server: Ubuntu22.04<br>
Snort Python: Python3.9+

## !!! # is for comments
## Please connect to the internet before installing the two servers
# Ryu server
```bash
sudo -s

apt install git -y
git clone https://github.com/sinyuan1022/NetDefender.git
cd ./NetDefender/ryu/

bash ./ryu_install.sh
```
# Snort server
```bash
sudo -s

apt install git -y
git clone https://github.com/sinyuan1022/NetDefender.git
cd ./NetDefender/snort/

bash ./snort_install.sh
```
