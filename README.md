# System environment
ryu server: Ubuntu22.04<br>
snort server: Ubuntu22.04

## !!! # is for comments

# ryu server
first install ovs and docker
```
apt update
apt upgrade -y
apt install openvswitch-switch docker.io vim net-tools isc-dhcp-server iptables-persistent dhcpcd5 htop ifmetric software-properties-common isc-dhcp-client git screen -y
add-apt-repository ppa:deadsnakes/ppa
apt update
git clone https://token@github.com/sinyuan1022/my-project.git
cd ./my-project/ryu/
apt install python3.9 python3.9-distutils -y
python3.9 get-pip.py
pip install setuptools==67.6.1 
pip install ryu docker
pip install eventlet==0.30.2
docker plugin install ghcr.io/devplayer0/docker-net-dhcp:release-linux-amd64
```
set Virtual NIC
```
ip link add veth0 type veth peer name veth1
ip addr add 192.168.100.1/24 dev veth0
ip link set veth0 up
ip link set veth1 up
ip link add my-bridge type bridge
ip link set my-bridge up
ip link set veth1 master my-bridge
iptables -A FORWARD -i my-bridge -j ACCEPT
iptables -I FORWARD -o my-bridge -j ACCEPT
```
enabling IPv4 Packet Forwarding
```
vim /etc/sysctl.conf
net.ipv4.ip_forward = 1 #updata
```
apply changes
```
sysctl -p
iptables -P FORWARD ACCEPT
```
set dhcp-server NIC
```
vim /etc/default/isc-dhcp-server

INTERFACESv4="veth0" #update
```
```
vim /etc/dhcp/dhcpd.conf

# Delete the following four lines from the original code
option domain-name "example.org"; 
option domain-name-servers ns1.example.org, ns2.example.org;
default-lease-time 600;
max-lease-time 7200;

# Add the following code where the previous code was deleted
subnet 192.168.100.0 netmask 255.255.255.0 {
  range 192.168.100.2 192.168.100.254; 
  option subnet-mask 255.255.255.0;
  option broadcast-address 192.168.100.255;
  option routers 192.168.100.1;
  ping-check true;
  default-lease-time 600;
  max-lease-time 7200;
 }
```
get ip
```
systemctl restart isc-dhcp-server
dhclient veth1
dhcpcd my-bridge
```
set ovs
```
bash ./setovs.sh ens33.  #ens33 is your ryu server NIC name
```
Disallow entry and exit of container for 67 and 68 areas
```
iptables -A FORWARD -i br0 -o my-bridge -p udp --dport 67 -j DROP
iptables -A FORWARD -i br0 -o my-bridge -p udp --dport 68 -j DROP
iptables -A FORWARD -i my-bridge -o br0 -p udp --sport 67 -j DROP
iptables -A FORWARD -i my-bridge -o br0 -p udp --sport 68 -j DROP
iptables -A FORWARD -i br0 -o veth0 -p udp --dport 67 -j DROP
iptables -A FORWARD -i br0 -o veth0 -p udp --dport 68 -j DROP
iptables -A FORWARD -i veth0 -o br0 -p udp --sport 67 -j DROP
iptables -A FORWARD -i veth0 -o br0 -p udp --sport 68 -j DROP
```
Run Ryu
```
ryu-manager ovs.py   #it is not run in background

screen -dmS ryu ryu-manager ovs.py   #it is run in background
```
set docker network
```
docker network create -d ghcr.io/devplayer0/docker-net-dhcp:release-linux-amd64 --ipam-driver null -o bridge=my-bridge my-dhcp-net
```
run container(Later, change it to automation.)
```
#Use two terminals to run
docker run --rm --name other --net=my-dhcp-net --cap-add=NET_ADMIN -v $(pwd):/captures ubuntu:latest tcpdump -i my-bridge0 -w /captures/capture_$(date +%Y%m%d%H%M%S).pcap
docker run --rm -ti --name ssh1 --network my-dhcp-net cowrie/cowrie
```
---
# snort server
install snort and python
```
apt install python3 python3-pip snort git vim net-tools -y
git clone https://token@github.com/sinyuan1022/my-project.git
cd ./my-project/snort

ifconfig ens33 promisc   #ens33 is your snort server NIC name
```
run snort 
```
# ens33 is your snort server NIC name
snort -i eth33 -A unsock -l /tmp -c /etc/snort/snort.conf   #it is not run in background

screen -dmS snort snort -i eth33 -A unsock -l /tmp -c /etc/snort/snort.conf   #it is run in background
```
set controller IP(run to background)
```
vim ./setting.py

CONTROLLER_IP = '192.168.2.179'   #change ryu server IP
```

set controller IP(not background)
```
vim ./pigrelay

CONTROLLER_IP = '127.0.0.1'   #change ryu server IP
```
run pigrelay
```
python3 pigrelay.py   #it is not run in background

python3 hpigrelay.py start   #it is run in background
```

