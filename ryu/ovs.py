from __future__ import print_function

import array
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import ether_types
from ryu.lib.packet import icmp
import snortlib
from ryu.lib.packet import packet, ethernet, ipv4, tcp, udp, arp
from ryu.ofproto import ether, inet
from ryu.lib import hub
from datetime import datetime
import hashlib
import subprocess
import re
import readconfig as rc
import dockerstart
import getip
from scapy.layers.l2 import Ether
from scapy.layers.inet import IP, ICMP
from scapy.utils import wrpcap
from newcontainer import *
from stopcontainer import *
import os
import docker


class SimpleSwitchSnort(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    _CONTEXTS = {'snortlib': snortlib.SnortLib}

    def __init__(self, *args, **kwargs):
        super(SimpleSwitchSnort, self).__init__(*args, **kwargs)
        self.snort = kwargs['snortlib']
        self.snort_port = 3
        self.mac_to_port = {}
        self.connection_map = {}
        self.connection_ip = {}
        socket_config = {'unixsock': False}
        self.dockerid = {}
        self.docker_config = rc.config()
        self.packet_store = []
        self.monitor_thread = hub.spawn(self._monitor)
        self.localIP = self.get_ip_address('br0')
        self.snort.set_config(socket_config)
        self.snort.start_socket_server()
        self.dockerstart = dockerstart.start()

        self.docker_client = docker.from_env()
        self.container_monitor = hub.spawn(self._container_monitor)
        self.container_status = {}  # {service_name: {container_name: {"last_used": timestamp, "ip": client_ip}}}
        self.ip_container_map = {}  # {service_name: {client_ip: container_name}}
        self.CONTAINER_TIMEOUT = 300  # 5分鐘
        self.initialize_services()

    def initialize_services(self):
        """初始化所有服務的容器管理"""
        for i, (port, configs) in enumerate(self.docker_config.items()):
            service_name = configs[0].get('name', f'service_{port}')
            self.container_status[service_name] = {}
            self.ip_container_map[service_name] = {}
            
            # 檢查容器是否已經運行
            container_name = f"{service_name}0"
            existing_containers = self.docker_client.containers.list(
                filters={"name": container_name}
            )
            
            if existing_containers:
                # 如果容器已存在，更新狀態
                self.container_status[service_name][container_name] = {
                    "last_used": datetime.now(),
                    "ip": None,
                    "is_primary": True,
                    "config": configs[0]
                }



    def get_ip_address(self,interface_name):
            try:
                result = subprocess.run(['ip', 'addr', 'show', interface_name],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                if result.returncode != 0:
                    print(f"Error: {result.stderr.strip()}")
                    return None

                match = re.search(r'inet\s+([\d.]+)', result.stdout)
                if match:
                    print(f"local ip:{match.group(1)}")
                    return match.group(1)
                else:
                    print("No IP address found.")
                    return None
            except Exception as e:
                print(f"Error: {e}")
                return None

    def _monitor(self):
        while True:
            while self.packet_store and (datetime.now() - self.packet_store[0][2]).total_seconds() > 3:
                pkt_hash, msg, timestamp = self.packet_store.pop()
                datapath = msg.datapath
                parser = datapath.ofproto_parser
                ofproto = datapath.ofproto
                in_port = msg.match['in_port']
                pkt = packet.Packet(msg.data)
                eth = pkt.get_protocols(ethernet.ethernet)[0]
                dst = eth.dst
                src = eth.src

                dpid = datapath.id
                self.mac_to_port.setdefault(dpid, {})

                self.mac_to_port[dpid][src] = in_port

                if dst in self.mac_to_port[dpid]:
                    out_port = self.mac_to_port[dpid][dst]
                else:
                    out_port = ofproto.OFPP_FLOOD
                actions = [parser.OFPActionOutput(out_port)]
                if out_port != ofproto.OFPP_FLOOD:
                    match = parser.OFPMatch(in_port=in_port, eth_dst=dst, eth_src=src)
                    if msg.buffer_id != ofproto.OFP_NO_BUFFER:
                        self.add_flow(datapath, 1, match, actions, msg.buffer_id)
                        return
                    else:
                        self.add_flow(datapath, 1, match, actions)

                
                # , parser.OFPActionOutput(self.snort_port)
                data = None
                if msg.buffer_id == ofproto.OFP_NO_BUFFER:
                    data = msg.data

                out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                          in_port=in_port, actions=actions, data=data)
                datapath.send_msg(out)
            hub.sleep(0.05)

    def _container_monitor(self):
        """監控容器使用狀況"""
        while True:
            current_time = datetime.now()
            
            for service_name, containers in self.container_status.items():
                for container_name, status in containers.items():
                    # 更新容器狀態
                    try:
                        container = self.docker_client.containers.get(container_name)
                        if container.status != "running":
                            # 如果是主要容器且沒在運行，重啟它
                            if status.get("is_primary", False):
                                self.logger.info(f"Restarting primary container {container_name}")
                                container.restart()
                            else:
                                # 如果是非主要容器且超時，移除它
                                if (current_time - status["last_used"]).total_seconds() > self.CONTAINER_TIMEOUT:
                                    self.logger.info(f"Removing inactive container {container_name}")
                                    container.remove(force=True)
                                    del self.container_status[service_name][container_name]
                                    if status["ip"] in self.ip_container_map[service_name]:
                                        del self.ip_container_map[service_name][status["ip"]]
                    except docker.errors.NotFound:
                        # 如果容器不存在且是主要容器，重新創建
                        if status.get("is_primary", False):
                            self.logger.info(f"Recreating primary container {container_name}")
                            start_new_container(f"{service_name}0", status["config"])
            
            hub.sleep(10)

    def get_available_container(self, client_ip, port):
        """為指定服務和客戶端 IP 分配容器"""
        service_name = f"port_{port}"
        if service_name not in self.container_status:
            self.logger.error(f"Unknown service for port {port}")
            return None

        # 如果該 IP 已有指定的容器
        if client_ip in self.ip_container_map[service_name]:
            container_name = self.ip_container_map[service_name][client_ip]
            if container_name in self.container_status[service_name]:
                self.update_container_timestamp(service_name, container_name)
                return container_name, self.container_status[service_name][container_name]["config"]

        # 檢查是否可以使用主要容器
        primary_container = f"{service_name}_0"
        if self.container_status[service_name][primary_container]["ip"] is None:
            self.container_status[service_name][primary_container]["ip"] = client_ip
            self.ip_container_map[service_name][client_ip] = primary_container
            self.update_container_timestamp(service_name, primary_container)
            return primary_container, self.container_status[service_name][primary_container]["config"]

        # 檢查服務是否支持多個容器
        service_config = self.docker_config[port][0]
        if service_config['multi'] == 'yes':
            return primary_container, service_config

        # 創建新的容器
        new_container_name = f"{service_name}_{len(self.container_status[service_name])}"
        if start_new_container(new_container_name, service_config):
            self.ip_container_map[service_name][client_ip] = new_container_name
            self.container_status[service_name][new_container_name] = {
                "last_used": datetime.now(),
                "ip": client_ip,
                "is_primary": False,
                "config": service_config
            }
            return new_container_name, service_config

        return None, None

    def update_container_timestamp(self, service_name, container_name):
        """更新容器的最後使用時間"""
        if service_name in self.container_status and container_name in self.container_status[service_name]:
            self.container_status[service_name][container_name]["last_used"] = datetime.now()
            self.logger.info(f"Updated timestamp for container {container_name}")

    def hash_packet(self,pkt):
        hash_parts = []

        eth_pkt = pkt.get_protocol(ethernet.ethernet)
        if eth_pkt:
            hash_parts.append(f"{eth_pkt.src}-{eth_pkt.dst}-{eth_pkt.ethertype}")

        ipv4_pkt = pkt.get_protocol(ipv4.ipv4)
        if ipv4_pkt:
            hash_parts.append(f"{ipv4_pkt.src}-{ipv4_pkt.dst}-{ipv4_pkt.proto}-{ipv4_pkt.identification}")

        if not hash_parts:
            return None

        combined_key = "|".join(hash_parts)
        return hashlib.md5(combined_key.encode()).hexdigest()


    @set_ev_cls(snortlib.EventAlert, MAIN_DISPATCHER)
    def _dump_alert(self, ev):
        msg = ev.msg
        pkt = packet.Packet(msg.pkt)
        ipv4_pkt = pkt.get_protocol(ipv4.ipv4)
        if (ipv4_pkt and ipv4_pkt.dst == self.localIP):
            pkt_hash = self.hash_packet(pkt)
            if pkt_hash is None:
                print("Invalid packet format.")
                return
            print(f"alert pkt:\n{pkt}\n{datetime.now()}\n")
            for i, (stored_hash, stored_pkt, timestamp) in enumerate(self.packet_store):
                if stored_hash == pkt_hash:
                    self.packet_store.pop(i)
                    print(f"Matching packet found: {pkt_hash}\n")
                    datapath = stored_pkt.datapath
                    in_port = stored_pkt.match['in_port']
                    pkt = packet.Packet(stored_pkt.data)
                    tcp_pkt = pkt.get_protocol(tcp.tcp)
                    if tcp_pkt:
                        if tcp_pkt.dst_port in self.docker_config:
                            self.handle_service_packet(pkt, datapath, in_port, msg, tcp_pkt.dst_port)
                            return
            self.alert_packet(pkt)
            return

    def alert_packet(self, pkt):
        ipv4_pkt = pkt.get_protocol(ipv4.ipv4)
        eth = pkt.get_protocol(ethernet.ethernet)
        icmp_pkt = pkt.get_protocol(icmp.icmp)

        output_dir = "./other"
        os.makedirs(output_dir, exist_ok=True)  # 如果資料夾不存在則自動建立

        target_ip = getip.getcontainer_ip("other")


        new_pkt = packet.Packet()
        if eth:
            new_pkt.add_protocol(ethernet.ethernet(
                ethertype=eth.ethertype,
                src=eth.src,
                dst=eth.dst
            ))
            scapy_pkt = Ether(src=eth.src, dst=eth.dst, type=eth.ethertype)
        if ipv4_pkt :
            new_ip_pkt = ipv4.ipv4(
                dst=target_ip,
                src=ipv4_pkt.src,
                proto=ipv4_pkt.proto
            )
            self.logger.info("Outgoing SSH traffic: %s -> %s",
                             ipv4_pkt.src, ipv4_pkt.dst)
            new_pkt.add_protocol(new_ip_pkt)
            if eth:
                scapy_pkt = Ether(src=eth.src, dst=eth.dst, type=eth.ethertype) / \
                            IP(src=ipv4_pkt.src, dst=target_ip, proto=ipv4_pkt.proto)
            else:
                scapy_pkt = IP(src=ipv4_pkt.src, dst=target_ip, proto=ipv4_pkt.proto)
        if icmp_pkt:
            new_pkt.add_protocol(icmp_pkt)
            if eth and ipv4_pkt:
                scapy_pkt =Ether(src=eth.src, dst=eth.dst, type=eth.ethertype) / \
                            IP(src=ipv4_pkt.src, dst=target_ip, proto=ipv4_pkt.proto) / \
                            ICMP(type=icmp_pkt.type, code=icmp_pkt.code)
            elif eth:
                scapy_pkt = Ether(src=eth.src, dst=eth.dst, type=eth.ethertype) / \
                            ICMP(type=icmp_pkt.type, code=icmp_pkt.code)
            elif ipv4_pkt:
                scapy_pkt = IP(src=ipv4_pkt.src, dst=target_ip, proto=ipv4_pkt.proto) / \
                            ICMP(type=icmp_pkt.type, code=icmp_pkt.code)
            else:
                scapy_pkt =ICMP(type=icmp_pkt.type, code=icmp_pkt.code)
        new_pkt.serialize()


        self.logger.info("Redirecting to: %s", target_ip)


        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        pcap_filename = os.path.join(output_dir, f"alert_packet_{timestamp}.pcap")
        wrpcap(pcap_filename, scapy_pkt)

        self.logger.info(f"Packet saved to PCAP: {pcap_filename}")


    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                          ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 0, match, actions)

    def add_flow(self, datapath, priority, match, actions, buffer_id=None):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS,
                                             actions)]
        if buffer_id:
            mod = parser.OFPFlowMod(datapath=datapath, buffer_id=buffer_id,
                                    priority=priority, match=match,
                                    instructions=inst)
        else:
            mod = parser.OFPFlowMod(datapath=datapath, priority=priority,
                                    match=match, instructions=inst)
        datapath.send_msg(mod)

    def handle_service_packet(self, pkt, datapath, in_port, msg, dst_port):
        """處理服務封包"""
        ipv4_pkt = pkt.get_protocol(ipv4.ipv4)
        tcp_pkt = pkt.get_protocol(tcp.tcp)
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto

        # 獲取服務配置
        service_config = self.docker_config.get(dst_port, [{}])[0]
        service_name = service_config.get('name', f'service_{dst_port}')
        
        # 獲取容器名稱
        container_name = f"{service_name}0"  # 使用主要容器
        
        # 更新使用時間
        if service_name in self.container_status and container_name in self.container_status[service_name]:
            self.container_status[service_name][container_name]["last_used"] = datetime.now()

        # 獲取容器 IP
        target_ip = getip.getcontainer_ip(container_name)
        if not target_ip:
            self.logger.error(f"Could not get IP for container {container_name}")
            return

        # 設置目標端口
        target_port = service_config.get('target_port', dst_port)

        # 建立連接映射
        self.connection_map[(ipv4_pkt.src, tcp_pkt.src_port)] = (ipv4_pkt.dst, tcp_pkt.dst_port)
        
        self.logger.info(f"Traffic on port {dst_port}: {ipv4_pkt.src}:{tcp_pkt.src_port} -> "
                        f"{target_ip}:{target_port} (Container: {container_name})")
        
        actions = [
            parser.OFPActionSetField(ipv4_dst=target_ip),
            parser.OFPActionSetField(tcp_dst=target_port),
            parser.OFPActionOutput(ofproto.OFPP_NORMAL)
        ]
        
        out = parser.OFPPacketOut(
            datapath=datapath, buffer_id=msg.buffer_id,
            in_port=in_port, actions=actions, data=msg.data
        )
        datapath.send_msg(out)

    def return_packet(self, pkt, datapath, in_port, msg):
        ipv4_pkt = pkt.get_protocol(ipv4.ipv4)
        tcp_pkt = pkt.get_protocol(tcp.tcp)
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto
        original_src = self.connection_map.get((ipv4_pkt.dst, tcp_pkt.dst_port))
        self.logger.info("Incoming redirected traffic: %s:%s -> %s:%s", 
                             ipv4_pkt.src, tcp_pkt.src_port, 
                             ipv4_pkt.dst, tcp_pkt.dst_port)
        if original_src:
            original_src_ip, original_src_port = original_src
            self.logger.info("Spoofing back to: %s:%s", original_src_ip, original_src_port)
            actions = [
                parser.OFPActionSetField(ipv4_src=original_src_ip),
                parser.OFPActionSetField(tcp_src=original_src_port),
                parser.OFPActionOutput(ofproto.OFPP_NORMAL)
            ]

            out = parser.OFPPacketOut(
                datapath=datapath, buffer_id=msg.buffer_id,
                in_port=in_port, actions=actions, data=msg.data
            )
            datapath.send_msg(out)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto
        in_port = msg.match['in_port']
        pkt = packet.Packet(msg.data)
        ipv4_pkt = pkt.get_protocol(ipv4.ipv4)
        tcp_pkt = pkt.get_protocol(tcp.tcp)
        if tcp_pkt and tcp_pkt:
            if tcp_pkt.dst_port == 22 and ipv4_pkt.dst == self.localIP:
                self.handle_service_packet(pkt, msg.datapath, msg.match['in_port'], msg, tcp_pkt.dst_port)
                return
            if tcp_pkt.src_port == 2222 or tcp_pkt.src_port == 2223:
                self.return_packet(pkt, datapath, in_port, msg)
                return
        if ipv4_pkt and self.snort.getsnortip():
            if ipv4_pkt.dst == self.snort.getsnortip() or ipv4_pkt.src == self.snort.getsnortip():
                datapath = msg.datapath
                in_port = msg.match['in_port']
                pkt = packet.Packet(msg.data)
                eth = pkt.get_protocols(ethernet.ethernet)[0]

                dst = eth.dst
                src = eth.src

                dpid = datapath.id
                self.mac_to_port.setdefault(dpid, {})

                self.mac_to_port[dpid][src] = in_port

                if dst in self.mac_to_port[dpid]:
                    out_port = self.mac_to_port[dpid][dst]
                else:
                    out_port = ofproto.OFPP_FLOOD
                actions = [parser.OFPActionOutput(out_port)]
                data = None
                if msg.buffer_id == ofproto.OFP_NO_BUFFER:
                    data = msg.data

                out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                          in_port=in_port, actions=actions, data=data)
                datapath.send_msg(out)
                return
            if (ipv4_pkt.dst == self.localIP):
                pkt_hash = self.hash_packet(pkt)
                current_time = datetime.now()
                if pkt_hash is None:
                    return
                self.packet_store.append((pkt_hash, msg, current_time))
                return

        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto
        in_port = msg.match['in_port']
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocols(ethernet.ethernet)[0]

        dst = eth.dst
        src = eth.src

        dpid = datapath.id
        self.mac_to_port.setdefault(dpid, {})

        self.mac_to_port[dpid][src] = in_port

        if dst in self.mac_to_port[dpid]:
            out_port = self.mac_to_port[dpid][dst]
        else:
            out_port = ofproto.OFPP_FLOOD

        actions = [parser.OFPActionOutput(out_port)]
        data = None
        if msg.buffer_id == ofproto.OFP_NO_BUFFER:
            data = msg.data

        out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                  in_port=in_port, actions=actions, data=data)
        datapath.send_msg(out)

        '''
        
        '''
        
    

        '''
        if out_port != ofproto.OFPP_FLOOD:
            match = parser.OFPMatch(in_port=in_port, eth_dst=dst, eth_src=src)
            if msg.buffer_id != ofproto.OFP_NO_BUFFER:
                self.add_flow(datapath, 1, match, actions, msg.buffer_id)
                return
            else:
                self.add_flow(datapath, 1, match, actions)
        '''

