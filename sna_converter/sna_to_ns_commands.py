#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""
sna_to_ns_commands.py
Takes a Cisco SNA / NetFlow CSV as input and outputs a Network Sketcher CLI
command sequence plus a [FLOW] paste-ready CSV. Runs on Windows / macOS / Ubuntu.

Input CSV formats (auto-detected):
  - API format : machine-readable columns such as searchSubject.* / peer.*
  - UI format  : human-readable columns such as Subject IP Address (Total Bytes="56.49 M",
                 Duration="36min 38s", Peer Port/Protocol="2055/UDP"). Auto-normalized to
                 the required columns before processing.

Design (NetFlow-only assumption):
  - Inside Hosts = RFC1918 + the organization's own public ranges (INSIDE_PUBLIC)
  - Observed IPs are aggregated into /24 subnets (= segment/VLAN) (flows>=THRESH)
  - Subnets are grouped into /16, then aggregated into sites via the traffic graph
    (nearest neighbor), with spurs split off
  - site_cidrs in sna_to_ns_config.json can explicitly map an arbitrary CIDR -> site
    (takes precedence over automatic aggregation)
  - server/client roles are decided by SNA orientation (peer = server)
  - sites are classified into "DC-like (server-oriented)" and "client-oriented"
  - Layout: (Internet-Svc) / Internet_wp_ / DC sites (top) / WAN_wp_ / client sites (bottom)

Output (per input CSV, under Output_data/<csv name>/):
  - gen_master_commands.txt : Network Sketcher CLI command sequence
  - gen_flow_list.csv        : [FLOW] paste-ready CSV (Source/Dest = master device name, Max bandwidth Mbps)
  - out_of_scope_ips.csv     : server-candidate IPs that were not adopted (with reasons)
  - _normalized_flow.csv     : intermediate normalized file for UI-format input

Endpoint registration (RULE 11.5 compliant = no SVI / IP directly on the physical port):
  --endpoints {none,servers,clients,both}   (default both)
      servers : inside server IP as 1 IP = 1 device. Internet-bound traffic is aggregated into one device per (proto,port)
      clients : 1 segment = 1 PC device (no IP)
      both    : both

Usage:
  python sna_to_ns_commands.py                         # convert all CSVs in Input_data at once
  python sna_to_ns_commands.py path/to/flow.csv        # convert a single CSV
  python sna_to_ns_commands.py path/to/folder          # convert all CSVs in a folder
  python sna_to_ns_commands.py --config my.json --endpoints both
"""
import csv, collections, sys, json, os, argparse, ipaddress, math, re, subprocess
sys.stdout.reconfigure(encoding="utf-8")

# ================= CONFIG =================
# Default config folder (same as the script). Can be overridden with --config.
BASEDIR = os.path.dirname(os.path.abspath(__file__))
THRESH = 100
# --- Defaults for manual overrides (empty = leave to auto-detection). Overridden by ns_config.json; manual wins on conflict ---
INSIDE_PUBLIC = ()                      # organization's own public ranges (treated as Inside). Empty = auto-estimate
K_CAMPUS = 2                            # number of client-site seeds (primary hubs)
DC_FORCE_REGIONS = set()               # /16 forced to be DC-like (manually added)
SPUR_REGIONS = set()                   # /16 forced to be an independent site (spur) (manually added)
MERGE_ALL_DC = True                     # merge DC-like /16s into a single DC site
NAME_MAP = {}                          # seed /16 -> site name (manual label, overrides auto name)
DC_SITE_NAME = "Datacenter"
SPUR_NAME = {}                          # spur /16 -> site name (manual; if absent, Site-<region> automatically)
CODE_MAP = {"Datacenter":"DC"}        # site name -> short code (manual; if absent, auto-shortened)
EP_ROW_WIDTH = 8                        # number of endpoints per row in the bottom of device_location
CHUNK = 40                              # split size for huge bulk commands (for MCP submission)
# --- Defaults for auto-detection parameters (overridable via ns_config.json) ---
AUTO_INSIDE = True                      # auto-estimate public Inside ranges ON/OFF
AUTO_DC = True                          # auto-detect DC sites ON/OFF
AUTO_SPUR = True                        # auto-detect spur sites ON/OFF
SPUR_LINK_RATIO = 0.05                  # spur test: isolated if max coupling with a seed < ratio*internal flows
# ==========================================

ap = argparse.ArgumentParser(
    description="Cisco SNA/NetFlow CSV -> Network Sketcher commands + [FLOW] CSV")
ap.add_argument("input", nargs="?", default=None,
                help="input CSV (single file) or folder. If omitted, all CSVs in --input-dir are processed")
ap.add_argument("--input-dir", default="Input_data",
                help="input folder for batch processing (default: Input_data)")
ap.add_argument("--output-dir", default="Output_data",
                help="output root (default: Output_data). Each CSV is written to <output-dir>/<csv name>/")
ap.add_argument("--endpoints", choices=["none","servers","clients","both"], default="both")
ap.add_argument("--server-min-flows", type=int, default=1)
ap.add_argument("--no-flow", action="store_true",
                help="do not generate the [FLOW] paste-ready CSV (gen_flow_list.csv)")
ap.add_argument("--config", default=None,
                help="path to the config JSON (default: sna_to_ns_config.json in the same folder as the script)")
ap.add_argument("--outdir", default=None,
                help="(internal) explicitly set the output folder for a single CSV")
args = ap.parse_args()

# ---- batch driver: when a folder / the default (Input_data) is given, process each CSV in its own process ----
def _is_csv(p): return p.lower().endswith(".csv")
single_file = args.input if (args.input and os.path.isfile(args.input)) else None
if single_file is None:
    folder = args.input if (args.input and os.path.isdir(args.input)) else args.input_dir
    if not os.path.isdir(folder):
        print("[ERROR] input folder not found: %s"%os.path.abspath(folder))
        print("        Place CSVs there, or specify the path to a single CSV."); sys.exit(1)
    csvs=sorted(os.path.join(folder,f) for f in os.listdir(folder)
                if _is_csv(f) and not f.startswith("_normalized_")
                and os.path.isfile(os.path.join(folder,f)))
    if not csvs:
        print("[ERROR] no CSV found: %s"%os.path.abspath(folder)); sys.exit(1)
    print("[BATCH] %d CSV file(s) in %s"%(len(csvs),os.path.abspath(folder)))
    rc=0
    for cp in csvs:
        stem=os.path.splitext(os.path.basename(cp))[0]
        od=os.path.join(args.output_dir, stem)
        cmd=[sys.executable, os.path.abspath(__file__), cp,
             "--outdir", od, "--output-dir", args.output_dir,
             "--endpoints", args.endpoints,
             "--server-min-flows", str(args.server_min_flows)]
        if args.config: cmd+=["--config", args.config]
        if args.no_flow: cmd.append("--no-flow")
        print("\n[BATCH] ==> %s  ->  %s"%(cp, od))
        r=subprocess.run(cmd); rc=rc or r.returncode
    print("\n[BATCH] done (%d file(s))."%len(csvs)); sys.exit(rc)

# ---- single-CSV processing (leaf) ----
CSV = single_file
DO_SRV = args.endpoints in ("servers","both")
DO_CLI = args.endpoints in ("clients","both")
MINF = args.server_min_flows
_stem = os.path.splitext(os.path.basename(CSV))[0]
OUTDIR = os.path.abspath(args.outdir) if args.outdir else os.path.abspath(os.path.join(args.output_dir, _stem))
os.makedirs(OUTDIR, exist_ok=True)
OUTFILE = os.path.join(OUTDIR, "gen_master_commands.txt")

# ---- auto-detect the input CSV format + normalize ----
# Supports: (1) API format (searchSubject.*/peer.* machine names): used as-is
#           (2) UI format (human-readable such as Subject IP Address, Total Bytes="56.49 M", Duration="36min 38s",
#               Peer Port/Protocol="2055/UDP"): converted to the required columns of the API format
def _parse_bytes(s):
    s=(s or "").strip()
    if not s or s=="--": return 0.0
    m=re.match(r"^([\d.]+)\s*([KMGTP]?)",s,re.I)
    if not m:
        try: return float(s)
        except: return 0.0
    mult={"":1,"K":1e3,"M":1e6,"G":1e9,"T":1e12,"P":1e15}.get(m.group(2).upper(),1)
    return float(m.group(1))*mult
def _parse_dur_ms(s):
    s=(s or "").strip().lower()
    if not s or s=="--": return 0
    tot=0.0
    for num,unit in re.findall(r"([\d.]+)\s*(d|h|hr|hours?|min|m|s|ms)",s):
        n=float(num)
        if unit=="d": tot+=n*86400
        elif unit in ("h","hr","hour","hours"): tot+=n*3600
        elif unit in ("min","m"): tot+=n*60
        elif unit=="ms": tot+=n/1000.0
        elif unit=="s": tot+=n
    return int(round(tot*1000))
def _parse_port(s):
    s=(s or "").strip()
    m=re.match(r"^(\d+)\s*/",s)
    if m: return int(m.group(1))
    return int(s) if s.isdigit() else -1
def normalize_csv(path):
    with open(path,newline="",encoding="utf-8",errors="replace") as fh:
        head=next(csv.reader(fh))
    if "searchSubject.ipAddress" in head: return path        # API format: use as-is
    if "Subject IP Address" not in head:  return path        # unknown format: leave to later stages
    ix={c:i for i,c in enumerate(head)}
    def g(v,c):
        j=ix.get(c); return v[j] if (j is not None and j<len(v)) else ""
    outp=os.path.join(OUTDIR,"_normalized_flow.csv")
    OUTCOLS=["searchSubject.ipAddress","peer.ipAddress","peer.portProtocol.port",
             "searchSubject.portProtocol.protocol","peer.synAckPackets",
             "connection.transferBytes","activeDuration"]
    with open(path,newline="",encoding="utf-8",errors="replace") as fh, \
         open(outp,"w",newline="",encoding="utf-8") as of:
        r=csv.reader(fh); next(r); w=csv.writer(of); w.writerow(OUTCOLS)
        for v in r:
            if not v or not g(v,"Subject IP Address"): continue
            try: sa=int(g(v,"Peer SYN/ACK Packets").strip())
            except: sa=0
            w.writerow([g(v,"Subject IP Address"), g(v,"Peer IP Address"),
                        _parse_port(g(v,"Peer Port/Protocol")),
                        (g(v,"protocol") or "").upper(), sa,
                        int(round(_parse_bytes(g(v,"Total Bytes")))),
                        _parse_dur_ms(g(v,"Duration"))])
    print("[INFO] UI-format CSV detected -> normalized:",outp)
    return outp
CSV = normalize_csv(CSV)

# ---- JSON config (optional overrides; falls back to code defaults) ----
# sna_to_ns_config.json has each key as a {"value": ..., "description": ...} structure.
# cfg() extracts value (also tolerates legacy flat values and meta keys such as _description).
cfgpath = os.path.abspath(args.config) if args.config else os.path.join(BASEDIR, "sna_to_ns_config.json")
CFG = {}
if os.path.exists(cfgpath):
    with open(cfgpath, encoding="utf-8") as f:
        CFG = json.load(f)
def cfg(key, default):
    v = CFG.get(key)
    if v is None: return default
    if isinstance(v, dict) and "value" in v: return v["value"]
    return v
SRV_MIN_BYTES = int(cfg("server_min_bytes", 5000))
REQ_SYNACK    = bool(cfg("require_tcp_synack", True))
INC_UDP       = bool(cfg("include_udp", True))
THRESH        = int(cfg("subnet_min_flows", THRESH))
# manual overrides (added to auto-detection; manual wins on conflict)
MAN_INSIDE = tuple(cfg("inside_public", list(INSIDE_PUBLIC)))
MAN_DC     = set(cfg("dc_force_regions", DC_FORCE_REGIONS))
MAN_SPUR   = set(cfg("spur_regions", SPUR_REGIONS))
NAME_MAP   = dict(cfg("name_map", NAME_MAP))
# auto-detection knobs
AUTO_INSIDE     = bool(cfg("auto_inside_public", AUTO_INSIDE))
AUTO_DC         = bool(cfg("auto_dc_regions", AUTO_DC))
AUTO_SPUR       = bool(cfg("auto_spur_regions", AUTO_SPUR))
SPUR_LINK_RATIO = float(cfg("spur_link_ratio", SPUR_LINK_RATIO))

# ---- CIDR-based site definition (explicitly assign sites by arbitrary CIDR; takes precedence over region auto-aggregation) ----
#   site_cidrs   : {"CIDR": "site name"}  e.g. {"10.10.0.0/16":"Tokyo","10.20.30.0/24":"SrvFarm"}
#   site_cidr_dc : list of the above site names to treat as DC (server-oriented / top row)
# The minimum aggregation unit is /24. A CIDR wider than /24 assigns all contained /24s; a CIDR finer
# than /24 assigns every overlapping /24, to that site (longest-prefix match wins).
SITE_CIDRS      = dict(cfg("site_cidrs", {}))
SITE_CIDR_DC    = set(cfg("site_cidr_dc", []))
CIDR_SITE_NAMES = set(SITE_CIDRS.values())
_site_nets=[]
for _cidr,_nm in SITE_CIDRS.items():
    try: _site_nets.append((ipaddress.ip_network(_cidr,strict=False),_nm))
    except ValueError: print("[WARN] invalid CIDR in site_cidrs: %r"%_cidr)
_site_nets.sort(key=lambda x:-x[0].prefixlen)   # longest prefix first
def cidr_site_ip(ip):
    try: a=ipaddress.ip_address(ip)
    except ValueError: return None
    for net,nm in _site_nets:
        if a in net: return nm
    return None
def cidr_site_sub(k24str):
    # site name of the longest-matching CIDR overlapping the /24 key ("a.b.c") (None if absent)
    if not k24str: return None
    try: s24=ipaddress.ip_network(k24str+".0/24",strict=False)
    except ValueError: return None
    for net,nm in _site_nets:
        if net.overlaps(s24): return nm
    return None

def dq(o): return json.dumps(o).replace('"',"'")
def wrap(lst,n): return [lst[i:i+n] for i in range(0,len(lst),n)]
def is_rfc1918(ip):
    if ip.startswith("10.") or ip.startswith("192.168."): return True
    if ip.startswith("172."):
        try: return 16<=int(ip.split(".")[1])<=31
        except: return False
    return False
def _oct(ip,i):
    try: return int(ip.split(".")[i])
    except: return -1
def is_special(ip):
    # reserved / special-use ranges that cannot be an organization's public range (RFC5735/6598/multicast, etc.)
    a=_oct(ip,0); b=_oct(ip,1)
    if a in (0,127): return True
    if a>=224: return True                               # multicast/reserved 224-255
    if ip.startswith("169.254."): return True            # link-local
    if a==100 and 64<=b<=127: return True                # CGNAT 100.64/10
    if a==198 and b in (18,19): return True              # benchmark 198.18/15
    if ip.startswith(("192.0.2.","198.51.100.","203.0.113.")): return True  # doc
    return False
def is_inside(ip):
    return is_rfc1918(ip) or any(ip.startswith(p) for p in INSIDE_PUBLIC)
def k24(ip):
    p=ip.split("."); return (p[0]+"."+p[1]+"."+p[2]) if len(p)==4 else None
def reg16(ip):
    p=ip.split("."); return (p[0]+"."+p[1]) if len(p)==4 else None
def last_oct(ip):
    try: return int(ip.split(".")[3])
    except: return -1

# ---------- AUTO: estimate the organization's public ranges (inside_public) (pre-scan before main processing) ----------
# An organization's public /16 is "dominated by outbound initiation (outinit)" = internal users/proxies send a lot outward.
#   outinit = number of flows that searchSubject (= the initiating side) sent to a public peer.
#   Servers on the Internet do not appear as the subject (outinit ~ 0).
# Decision: excluding special-use ranges, adopt only /16s with outinit >= absolute threshold AND outinit/total >= ratio.
# (scattered external clients and popular services are excluded by the ratio/absolute value)
INSIDE_OUTINIT_MIN   = int(cfg("inside_outinit_min", 2000))
INSIDE_OUTINIT_RATIO = float(cfg("inside_outinit_ratio", 0.6))
auto_inside=set()
if AUTO_INSIDE:
    pub_oi=collections.Counter(); pub_tot=collections.Counter()
    with open(CSV,newline="",encoding="utf-8",errors="replace") as fh:
        r=csv.reader(fh); cols=next(r); ix={c:i for i,c in enumerate(cols)}
        Sip=ix["searchSubject.ipAddress"];Pip=ix["peer.ipAddress"]
        mx=max(Sip,Pip)
        for v in r:
            if len(v)<=mx: continue
            sip=v[Sip];pip=v[Pip]
            rs=is_rfc1918(sip); rp=is_rfc1918(pip)
            if not rs and not is_special(sip):
                rg=reg16(sip); pub_tot[rg]+=1
                if not rp: pub_oi[rg]+=1            # public subject -> public peer = internal outbound
            if not rp and not is_special(pip):
                pub_tot[reg16(pip)]+=1
    for rg,oi in pub_oi.items():
        if oi>=INSIDE_OUTINIT_MIN and oi>=INSIDE_OUTINIT_RATIO*pub_tot[rg]:
            auto_inside.add(rg+".")
# merge manual (manual wins on conflict = always include manual in the union)
INSIDE_PUBLIC = tuple(sorted(set(auto_inside) | set(MAN_INSIDE)))

# real service-port test for external (Internet) services (as before: not used for inside-server detection)
KNOWN_HIGH={1433,1521,3306,3389,5432,5060,5061,8080,8443,8000,5989,5985,5986,
            1645,1812,1813,9100,52311,7778,10000,3268,3269,2049}
def is_service(p): return p>0 and (p<1024 or p in KNOWN_HIGH)

# ---------- per /24 features + endpoint detection ----------
class Sub:
    __slots__=("flows","bytes","octs","srv","cli","reg")
    def __init__(s,reg): s.flows=0;s.bytes=0.0;s.octs=set();s.srv=0;s.cli=0;s.reg=reg
sub={}
mat=collections.Counter()        # (regA,regB) inter-region flows
regflows=collections.Counter()   # region total internal flows
ports_bytes=collections.defaultdict(collections.Counter)  # inside server IP -> {port: transferBytes}
ports_flows=collections.defaultdict(collections.Counter)  # inside server IP -> {port: flows}
srv_ip_clients=collections.defaultdict(set)               # inside server IP -> {client ip,...}
svc_bytes=collections.Counter()      # (proto,port) external service -> bytes
svc_flows=collections.Counter()      # (proto,port) external service -> flows
svc_ips=collections.defaultdict(set) # (proto,port) external service -> {server IP,...}
seg_cli_hosts=collections.defaultdict(set)   # /24 segment -> {client (initiator) side host IP,...}

with open(CSV,newline="",encoding="utf-8",errors="replace") as fh:
    r=csv.reader(fh); cols=next(r); ix={c:i for i,c in enumerate(cols)}
    # orientation fixed: searchSubject = client, peer = server
    Sip=ix["searchSubject.ipAddress"];Pip=ix["peer.ipAddress"]
    Pp=ix["peer.portProtocol.port"]                      # server (= service) port
    PrS=ix["searchSubject.portProtocol.protocol"]        # L4 protocol of the flow
    pSA=ix["peer.synAckPackets"]                          # evidence that the server accepted the connection
    By=ix["connection.transferBytes"]
    maxix=max(Sip,Pip,Pp,PrS,pSA,By)
    for v in r:
        if len(v)<=maxix: continue
        sip=v[Sip];pip=v[Pip]                            # sip=client, pip=server
        proto=(v[PrS] or "").upper()
        try: sport=int(v[Pp])
        except: sport=-1
        try: by=float(v[By])
        except: by=0.0
        try: psa=int(v[pSA])
        except: psa=0
        ina,inb=is_inside(sip),is_inside(pip)
        # inter-region matrix (internal-internal)
        if ina and inb:
            ra,rb=reg16(sip),reg16(pip)
            if ra and rb:
                regflows[ra]+=1; regflows[rb]+=1
                if ra!=rb: mat[tuple(sorted((ra,rb)))]+=1
        # server-flow acceptance (orientation + handshake/proto)
        if proto=="TCP":
            ok = (psa>0) if REQ_SYNACK else True
        elif proto=="UDP":
            ok = INC_UDP
        else:
            ok = False                                   # ICMP etc. are out of scope for services
        # endpoint detection (server = peer side)
        if ok and sport>0:
            if inb:                                      # inside server
                ports_bytes[pip][sport]+=by
                ports_flows[pip][sport]+=1
                srv_ip_clients[pip].add(sip)
            elif ina and is_service(sport):              # external (Internet) service (real service ports only)
                svc_bytes[(proto,sport)]+=by
                svc_flows[(proto,sport)]+=1
                svc_ips[(proto,sport)].add(pip)          # record the external server IP of this service
        # /24 aggregation + role (sip=client, pip=server)
        if ina:
            kk=k24(sip); e=sub.get(kk)
            if e is None: e=Sub(reg16(sip)); sub[kk]=e
            e.flows+=1; e.bytes+=by; e.cli+=1
            seg_cli_hosts[kk].add(sip)           # population of client (initiator) hosts per segment
            try: e.octs.add(int(sip.split(".")[3]))
            except: pass
        if inb:
            kk=k24(pip); e=sub.get(kk)
            if e is None: e=Sub(reg16(pip)); sub[kk]=e
            e.flows+=1; e.bytes+=by
            if ok: e.srv+=1
            try: e.octs.add(int(pip.split(".")[3]))
            except: pass

adopted={k:e for k,e in sub.items() if e.flows>=THRESH}

# ---------- region features ----------
reg_members=collections.defaultdict(list)
for k,e in adopted.items(): reg_members[e.reg].append(k)
def is_userlan(e):
    hosts=len(e.octs); gw=1 in e.octs
    return hosts>=20 and e.cli>e.srv and gw
reg_user=collections.Counter(); reg_srv=collections.Counter(); reg_cli=collections.Counter()
for reg,ks in reg_members.items():
    for k in ks:
        e=adopted[k]
        reg_srv[reg]+=e.srv; reg_cli[reg]+=e.cli
        if is_userlan(e): reg_user[reg]+=1

regions=set(reg_members)
# --- AUTO: DC-site detection (number of server segments > number of user segments) + manual force ---
def reg_is_dc_auto(reg):
    ks=reg_members[reg]
    n_srv_seg=sum(1 for k in ks if adopted[k].srv>adopted[k].cli)
    n_user_seg=len(ks)-n_srv_seg  # number of segments where cli >= srv
    return n_srv_seg>n_user_seg   # do not treat as DC when equal
auto_dc=set(r for r in regions if AUTO_DC and reg_is_dc_auto(r))
dc_regs=auto_dc | set(r for r in regions if r in MAN_DC)   # manual wins (always included)
non_dc=[r for r in regions if r not in dc_regs]

# seeds (primary hubs) = top K by internal flows, excluding DC
seeds=sorted(non_dc,key=lambda r:-regflows.get(r,0))[:K_CAMPUS]
def best_link(reg):
    return max((mat.get(tuple(sorted((reg,s))),0) for s in seeds if s!=reg), default=0)
# --- AUTO: spur (isolated) site detection: weakly coupled to every seed + manual force ---
auto_spur=set()
if AUTO_SPUR:
    for r in non_dc:
        if r in seeds: continue
        rf=regflows.get(r,0)
        if rf>0 and best_link(r) < SPUR_LINK_RATIO*rf:
            auto_spur.add(r)
spur_regs=(auto_spur | set(r for r in regions if r in MAN_SPUR)) - set(seeds) - dc_regs
campus_regs=[r for r in non_dc if r not in spur_regs]

def strongest_seed(reg):
    best=None;bw=-1
    for s in seeds:
        w=mat.get(tuple(sorted((reg,s))),0)
        if reg==s: w=10**9
        if w>bw: bw=w;best=s
    return best if best is not None else reg

region_site={}
for reg in campus_regs:
    region_site[reg]=NAME_MAP.get(strongest_seed(reg), "Campus-%s"%strongest_seed(reg))
for reg in dc_regs:
    region_site[reg]=DC_SITE_NAME if MERGE_ALL_DC else ("DC-%s"%reg)
for reg in spur_regs:
    region_site[reg]=SPUR_NAME.get(reg,"Site-%s"%reg.replace(".","-"))

# subnet (/24) -> site. If an explicit CIDR (site_cidrs) exists, it takes precedence over region auto-aggregation.
site_subnets=collections.defaultdict(list)
site_of_sub={}
for k,e in adopted.items():
    s=cidr_site_sub(k)
    if s is None: s=region_site[e.reg]
    site_of_sub[k]=s; site_subnets[s].append((k,e))
# site -> member /16 (rebuilt from site_subnets for summary display)
site_regs=collections.defaultdict(list)
for s,subs in site_subnets.items():
    site_regs[s]=sorted(set(e.reg for k,e in subs))

def site_is_dc(site):
    if site in SITE_CIDR_DC: return True               # explicit DC designation for a CIDR site
    subs=site_subnets[site]
    regs=set(e.reg for k,e in subs)
    if regs and regs<=dc_regs and site not in CIDR_SITE_NAMES: return True
    n_srv_seg=sum(1 for k,e in subs if e.srv>e.cli)
    n_user_seg=sum(1 for k,e in subs if e.cli>=e.srv)
    return n_srv_seg>n_user_seg    # do not treat as DC when equal
dc_sites=sorted([s for s in site_subnets if site_is_dc(s)])
client_sites=[s for s in site_subnets if not site_is_dc(s)]
def site_flow(s): return sum(e.flows for k,e in site_subnets[s])
client_sites=sorted(client_sites,key=lambda s:-site_flow(s))

# Fallback 1: no DC -> make the site with the most server-side flows the DC
if not dc_sites and client_sites:
    def _site_srv_flows(s):
        return sum(e.srv for k,e in site_subnets[s])
    _best=max(client_sites,key=_site_srv_flows)
    if _site_srv_flows(_best)>0:
        dc_sites=[_best]
        client_sites=[s for s in client_sites if s!=_best]

# Fallback 2: no servers at all -> make the site with the most client IPs the DC
if not dc_sites and client_sites:
    def _site_cli_ips(s):
        return sum(len(seg_cli_hosts.get(k,())) for k,e in site_subnets[s])
    _best=max(client_sites,key=_site_cli_ips)
    dc_sites=[_best]
    client_sites=[s for s in client_sites if s!=_best]

client_set=set(client_sites)
site_order=dc_sites+client_sites

# unique short code per site (for infra device names). A suffix is added to avoid collisions in auto-naming.
def _basecode(site):
    c=CODE_MAP.get(site)
    if c: return c
    return ("".join(ch for ch in site if ch.isalnum()).upper()[:6]) or "SITE"
_code_used=collections.Counter(); _code_of={}
for _s in site_order:
    _b=_basecode(_s)
    _code_of[_s]=_b if _code_used[_b]==0 else "%s%d"%(_b,_code_used[_b])
    _code_used[_b]+=1
def code(site): return _code_of[site]

# 4-char unique area code for endpoint naming
def build_acode(names):
    used=collections.Counter(); res={}
    for nm in names:
        base="".join(ch for ch in nm if ch.isalnum())[:4] or "Area"
        if used[base]==0: res[nm]=base
        else: res[nm]=base+str(used[base])
        used[base]+=1
    return res
acode=build_acode(site_order)

# ---------- VLAN assignment (deterministic, per site by flows desc) ----------
seg_vlan={}; site_svis={}
vlan=101
for s in site_order:
    out=[]
    for k,e in sorted(site_subnets[s],key=lambda x:-x[1].flows):
        seg_vlan[k]=vlan; out.append((k,e,vlan)); vlan+=1
    site_svis[s]=out

# ---------- endpoint sets ----------
def site_access(site):
    c=code(site)
    return (c+"-Acc1") if site in client_set else (c+"-Core")

servers=[]   # (name, ip, vlanname, site)
srv_meta={}  # ip -> (max_port_bytes, total_bytes, top_port, distinct_clients)  (for out-of-scope records)
oos=[]       # out-of-scope: (ip, region, reason, max_port_bytes, total_bytes, top_port, distinct_clients)
n_cand=0     # number of server-candidate IPs by orientation
if DO_SRV:
    per=collections.Counter()   # (site, port-label) -> sequence number
    cand=sorted(ports_bytes.keys(),
                key=lambda ip:(-sum(ports_bytes[ip].values()), ip))
    for ip in cand:
        n_cand+=1
        pb=ports_bytes[ip]
        tot=int(sum(pb.values())); mx=int(max(pb.values()) if pb else 0)
        topp=max(pb.items(),key=lambda x:x[1])[0] if pb else -1
        ncl=len(srv_ip_clients.get(ip,()))
        # adopt only real service ports above the bytes threshold (+ optional flow floor MINF)
        qports=sorted(p for p,b in pb.items()
                      if b>=SRV_MIN_BYTES and ports_flows[ip][p]>=MINF)
        if not qports:
            oos.append((ip,reg16(ip),"below_traffic_threshold",mx,tot,topp,ncl)); continue
        seg=k24(ip)
        if seg not in adopted or last_oct(ip)==1:
            oos.append((ip,reg16(ip),"segment_not_adopted_or_gateway",mx,tot,topp,ncl)); continue
        site=site_of_sub.get(seg) or region_site.get(reg16(ip))
        plabel="-".join(str(p) for p in qports)
        per[(site,plabel)]+=1
        servers.append(("SRV_%s_%s_%d"%(acode[site],plabel,per[(site,plabel)]),
                        ip, "Vlan%d"%seg_vlan[seg], site))
        srv_meta[ip]=(mx,tot,topp,ncl)

# ---------- segment classification (server-segment vs client-segment) ----------
# When servers and clients are mixed in the same /24 segment, assign it to "whichever has more hosts".
#   server-segment : adopted server count > client-only host count
#   client-segment : otherwise (client >= server, or zero servers)
# server-segment generates the server group (separated under a FW later); client-segment generates one PC.
seg_srv_hosts=collections.defaultdict(set)
for nm,ip,vn,st in servers: seg_srv_hosts[k24(ip)].add(ip)
def _seg_is_server(seg):
    ns=len(seg_srv_hosts.get(seg,()))
    nc=len(seg_cli_hosts.get(seg,set())-seg_srv_hosts.get(seg,set()))
    return ns>0 and ns>nc
server_seg=set(k for k in adopted if _seg_is_server(k))
# move server candidates that were in a client-majority segment to out-of-scope (not separated)
kept=[]
for nm,ip,vn,st in servers:
    if k24(ip) in server_seg: kept.append((nm,ip,vn,st))
    else:
        mx,tot,topp,ncl=srv_meta.get(ip,(0,0,-1,0))
        oos.append((ip,reg16(ip),"client_majority_segment",mx,tot,topp,ncl))
servers=kept

pcs=[]       # (name, vlanname, site)
pc_name_by_seg={}   # /24 segment -> PC device name (for resolving IP -> name in the flow CSV)
if DO_CLI:
    per=collections.Counter()
    for s in site_order:
        for k,e,vl in site_svis[s]:
            if k in server_seg: continue          # do not create a PC for a server-segment
            if e.cli<=0: continue                 # exclude segments with no client activity
            per[s]+=1
            n_cli_ips=len(seg_cli_hosts.get(k,()))
            nm="PC_%s_%d_%d"%(acode[s],n_cli_ips,per[s])
            pcs.append((nm, "Vlan%d"%vl, s)); pc_name_by_seg[k]=nm

svcs=[]      # (name, proto, port, flows)
svc_oos=0    # number of external services excluded below the threshold
# reserve the next VLAN number for the Internet WayPoint shared segment
inet_vlan=vlan; vlan+=1   # vlan is the next free number after all internal subnets are assigned
INET_VLAN_NAME="VlanIntSvc"
if DO_SRV:
    for (proto,port),b in sorted(svc_bytes.items(),key=lambda x:-x[1]):
        fl=svc_flows[(proto,port)]
        if b<SRV_MIN_BYTES or fl<MINF:
            svc_oos+=1; continue
        n_ips=len(svc_ips[(proto,port)])
        svcs.append(("Svc_%s%d_%d"%(proto,port,n_ips), proto, port, fl))

# ---------- server/PC separation helpers (FW between server & client segments) ----------
srv_sites=set(st for nm,ip,vn,st in servers)
pc_sites =set(st for nm,vn,st in pcs)
def site_mixed(s):
    # a client site where a server segment and a client segment coexist in the same area
    return (s in srv_sites) and (s in pc_sites) and (s in client_set)
def srv_access(site):
    return (code(site)+"-SrvSw") if site_mixed(site) else site_access(site)
def cli_access(site):
    return site_access(site)

def mixed_client_grid(site):
    # device_location for a mixed client site. A new SrvSw is placed under the boundary FW to separate servers:
    #   left column (col0) vertical : Edge - FW - Core - Acc1 - (PCs wrap downward by EP_ROW_WIDTH)
    #   right-side column           : SrvSw (same row as Core) - (servers stacked vertically in one column)
    # The PC band (left) and the server band (right) are separated by an empty column to avoid L1 link crossings.
    c=code(site)
    pcs_s=[nm for nm,vn,st in pcs if st==site]
    srv_s=[nm for nm,ip,vn,st in servers if st==site]
    Wpc=max(1,min(len(pcs_s),EP_ROW_WIDTH))
    Wsrv=max(1,min(len(srv_s),EP_ROW_WIDTH))
    col_srv=Wpc+1                          # start column of the server band (one empty column between it and the PC band)
    g={}
    g[(0,0)]="%s-Edge"%c
    g[(1,0)]="%s-FW"%c
    g[(2,0)]="%s-Core"%c
    g[(2,col_srv)]="%s-SrvSw"%c
    g[(3,0)]="%s-Acc1"%c
    for i,nm in enumerate(pcs_s):          # PC: wrap left-aligned directly under Acc1 (r4-)
        g[(4+i//Wpc, i%Wpc)]=nm
    for i,nm in enumerate(srv_s):          # server: wrap within the server band directly under SrvSw (r3-)
        g[(3+i//Wsrv, col_srv+(i%Wsrv))]=nm
    maxr=max(r for r,_ in g); maxc=max(cc for _,cc in g)
    return [[g.get((r,cc),"_AIR_") for cc in range(maxc+1)] for r in range(maxr+1)]

# ---------- build commands ----------
cmds=[]
# 1) area_location
grid=[]
if DO_SRV and svcs: grid.append(["Internet-Svc"])
grid.append(["Internet_wp_"])
if dc_sites: grid.append(list(dc_sites))
grid.append(["WAN_wp_"])
grid.append(list(client_sites))
cmds.append('add area_location "%s"'%dq(grid))

# 2) device_location (infra + endpoints at bottom)
def eps_of(site):
    return [nm for nm,ip,vn,st in servers if st==site]+[nm for nm,vn,st in pcs if st==site]
for s in dc_sites:
    c=code(s); rows=[["%s-FW"%c],["%s-Core"%c]]+wrap(eps_of(s),EP_ROW_WIDTH)
    cmds.append('add device_location "%s"'%dq([s,rows]))
for s in client_sites:
    if site_mixed(s):
        cmds.append('add device_location "%s"'%dq([s,mixed_client_grid(s)]))
    else:
        c=code(s); rows=[["%s-Edge"%c],["%s-FW"%c],["%s-Core"%c],["%s-Acc1"%c]]+wrap(eps_of(s),EP_ROW_WIDTH)
        cmds.append('add device_location "%s"'%dq([s,rows]))
# Internet-Svc: the Internet (waypoint) sits directly under the area, so place all servers in a single
# bottom row and connect them downward (multiple rows would make upper servers' lines cross the lower ones).
if DO_SRV and svcs:
    cmds.append('add device_location "%s"'%dq(["Internet-Svc",[[nm for nm,_,_,_ in svcs]]]))

# 3) L1 links (infra + endpoints + svc)
links=[]; wan_p=0; inet_p=0
for s in dc_sites:
    c=code(s)
    links.append(["%s-Core"%c,"%s-FW"%c,"GigabitEthernet 0/1","GigabitEthernet 0/1"])
    links.append(["%s-FW"%c,"Internet","GigabitEthernet 0/2","port %d"%inet_p]); inet_p+=1
    links.append(["%s-Core"%c,"WAN","GigabitEthernet 0/2","port %d"%wan_p]); wan_p+=1
for s in client_sites:
    c=code(s)
    links.append(["%s-Edge"%c,"%s-FW"%c,"GigabitEthernet 0/1","GigabitEthernet 0/1"])
    links.append(["%s-FW"%c,"%s-Core"%c,"GigabitEthernet 0/2","GigabitEthernet 0/2"])
    links.append(["%s-Core"%c,"%s-Acc1"%c,"GigabitEthernet 0/1","GigabitEthernet 0/1"])
    links.append(["%s-Edge"%c,"WAN","GigabitEthernet 0/2","port %d"%wan_p]); wan_p+=1
    if site_mixed(s):                      # connect the server SW under the boundary FW (server<->PC goes through the FW)
        links.append(["%s-FW"%c,"%s-SrvSw"%c,"GigabitEthernet 0/3","GigabitEthernet 0/1"])
# endpoints: connect to access switch; remember switch-port for L2 access binding
#   server -> srv_access (SrvSw for mixed sites, otherwise the usual access) / PC -> cli_access
sw_n=collections.Counter(); ep_access=[]   # (sw, swport, vlanname)
for nm,ip,vn,st in servers:
    sw=srv_access(st); sw_n[sw]+=1; swp="GigabitEthernet 1/0/%d"%sw_n[sw]
    links.append([nm,sw,"GigabitEthernet 0/0",swp]); ep_access.append((sw,swp,vn))
for nm,vn,st in pcs:
    sw=cli_access(st); sw_n[sw]+=1; swp="GigabitEthernet 1/0/%d"%sw_n[sw]
    links.append([nm,sw,"GigabitEthernet 0/0",swp]); ep_access.append((sw,swp,vn))
inet_p_svc_start=inet_p   # record the starting port number on the Internet side used by Svc devices
for nm,proto,port,fl in svcs:
    links.append([nm,"Internet","GigabitEthernet 0/0","port %d"%inet_p]); inet_p+=1
for batch in wrap(links,CHUNK):
    cmds.append('add l1_link_bulk "%s"'%dq(batch))

# 4) port_info (all devices 1Gbps; waypoints N/A)
alldev=[]
for s in dc_sites: c=code(s); alldev+=["%s-Core"%c,"%s-FW"%c]
for s in client_sites:
    c=code(s); alldev+=["%s-Edge"%c,"%s-FW"%c,"%s-Core"%c,"%s-Acc1"%c]
    if site_mixed(s): alldev.append("%s-SrvSw"%c)
alldev+=[nm for nm,_,_,_ in servers]+[nm for nm,_,_ in pcs]+[nm for nm,_,_,_ in svcs]
for batch in wrap(alldev,CHUNK):
    cmds.append('rename port_info_bulk "%s"'%dq([[batch,"_ALL_",["1Gbps","Full","1000BASE-T"]]]))
cmds.append('rename port_info_bulk "%s"'%dq([[["WAN","Internet"],"_ALL_",["N/A","N/A","N/A"]]]))

# --- Attribute sheet: color constants & helpers ---
# Palette shared with cv_converter / cml_converter (observed=green / inferred=gray).
C_INFRA  = [200, 200, 200]  # light gray   — inferred/synthesised devices + inferred WayPoints
C_SERVER = [255, 204, 204]  # light red    — server-role endpoints (inside + internet)
C_PC     = [255, 255, 204]  # very light yellow — client endpoints
C_NET    = [235, 241, 222]  # light green  — OBSERVED (real) network gear
C_WP_OBS = [220, 230, 242]  # light blue   — observed network-device WayPoint (reserved; not emitted today)

# Network Sketcher stencils that denote network gear (vs server/client endpoints).
_NET_STENCILS = {"Router", "L3Switch", "Switch", "Firewall", "WLC", "AP"}

def _role_color(stencil, observed=False):
    """Default-cell colour by role, unified across the sna / cv / cml converters.

    observed=green / inferred=gray. SNA synthesises all of its network gear from
    the flow data, so network stencils pass observed=False and render gray; the
    green branch is reserved for any network device the flow data surfaces as
    real (kept consistent with cv_/cml_converter).
    """
    if stencil == "Server":
        return C_SERVER
    if stencil == "PC":
        return C_PC
    if stencil in _NET_STENCILS:
        return C_NET if observed else C_INFRA
    return C_INFRA  # Cloud / WayPoint / unknown

def _strip_nums(m):
    """Remove tokens containing digits: 'Catalyst 9300' → 'Catalyst'."""
    return ' '.join(w for w in m.split() if not any(c.isdigit() for c in w))

def _ac(text, rgb):
    """Return escaped attribute cell string: \\\"['text',[R,G,B]]\\\"."""
    return "\\\"['%s',[%d,%d,%d]]\\\"" % (text, rgb[0], rgb[1], rgb[2])

def _attr_row_str(row):
    """Format one attr_row as the inner list string for rename_attribute_bulk."""
    name, defv, model, os_val, stencil = row
    parts = ["'%s'" % name]
    if isinstance(defv, (list, tuple)):   # colored cell
        parts.append(_ac(defv[0], defv[1]))
    else:                                  # plain string (WayPoint / header)
        parts.append("'%s'" % defv)
    parts.append("'%s'" % _strip_nums(model))
    parts.append("'%s'" % os_val)
    parts.append("'%s'" % stencil)
    return '[%s]' % ','.join(parts)

# 5) SVI/L2/IP per site (SVIs on site Core)
attr_rows=[["Device Name","Default","Model","OS","Stencil Type"]]
for s in dc_sites:
    c=code(s); core="%s-Core"%c
    svis=[]; binds=[]; ips=[]
    for k,e,vl in site_svis[s]:
        svis.append("Vlan %d"%vl); binds.append([core,"Vlan %d"%vl,["Vlan%d"%vl]])
        ips.append([core,"Vlan %d"%vl,[k+".1/24"]])
    cmds.append('add virtual_port_bulk "%s"'%dq([[core,svis]]))
    cmds.append('add l2_segment_bulk "%s"'%dq(binds))
    cmds.append('add ip_address_bulk "%s"'%dq(ips))
    attr_rows.append([core,("DEVICE",_role_color("L3Switch")),"Nexus 9336C","NX-OS","L3Switch"])
    attr_rows.append(["%s-FW"%c,("DEVICE",_role_color("Firewall")),"Secure Firewall 4115","FTD","Firewall"])
for s in client_sites:
    c=code(s); core="%s-Core"%c; acc="%s-Acc1"%c; fw="%s-FW"%c; ssw="%s-SrvSw"%c
    mx=site_mixed(s)
    # in mixed sites, the server segment's SVI goes on the FW (= server<->client separated by the FW).
    cli_segs=[(k,e,vl) for (k,e,vl) in site_svis[s] if not (mx and k in server_seg)]
    srv_segs=[(k,e,vl) for (k,e,vl) in site_svis[s] if (mx and k in server_seg)]
    # client segments: SVI on Core, trunk Core<->Acc1
    csvis=[]; cbinds=[]; cips=[]; cvn=[]
    for k,e,vl in cli_segs:
        csvis.append("Vlan %d"%vl); cbinds.append([core,"Vlan %d"%vl,["Vlan%d"%vl]])
        cvn.append("Vlan%d"%vl); cips.append([core,"Vlan %d"%vl,[k+".1/24"]])
    if csvis:
        cmds.append('add virtual_port_bulk "%s"'%dq([[core,csvis]]))
        cmds.append('add l2_segment_bulk "%s"'%dq(cbinds))
        cmds.append('add l2_segment_bulk "%s"'%dq([[core,"GigabitEthernet 0/1",cvn],
                                                   [acc,"GigabitEthernet 0/1",cvn]]))
        cmds.append('add ip_address_bulk "%s"'%dq(cips))
    # server segments (mixed only): SVI on FW, trunk FW<->SrvSw
    if mx and srv_segs:
        ssvis=[]; sbinds=[]; sips=[]; svn=[]
        for k,e,vl in srv_segs:
            ssvis.append("Vlan %d"%vl); sbinds.append([fw,"Vlan %d"%vl,["Vlan%d"%vl]])
            svn.append("Vlan%d"%vl); sips.append([fw,"Vlan %d"%vl,[k+".1/24"]])
        cmds.append('add virtual_port_bulk "%s"'%dq([[fw,ssvis]]))
        cmds.append('add l2_segment_bulk "%s"'%dq(sbinds))
        cmds.append('add l2_segment_bulk "%s"'%dq([[fw,"GigabitEthernet 0/3",svn],
                                                   [ssw,"GigabitEthernet 0/1",svn]]))
        cmds.append('add ip_address_bulk "%s"'%dq(sips))
    attr_rows.append(["%s-Edge"%c,("DEVICE",_role_color("Router")),"Catalyst 8300","IOS-XE","Router"])
    attr_rows.append([fw,("DEVICE",_role_color("Firewall")),"Secure Firewall 3120","FTD","Firewall"])
    attr_rows.append([core,("DEVICE",_role_color("L3Switch")),"Catalyst 9500","IOS-XE","L3Switch"])
    attr_rows.append([acc,("DEVICE",_role_color("Switch")),"Catalyst 9300","IOS-XE","Switch"])
    if mx: attr_rows.append([ssw,("DEVICE",_role_color("Switch")),"Catalyst 9300","IOS-XE","Switch"])

# 6) endpoint access L2 (switch side, 1 VLAN) + server physical-port IP (RULE 11.5)
for batch in wrap([[sw,swp,[vn]] for (sw,swp,vn) in ep_access],CHUNK):
    cmds.append('add l2_segment_bulk "%s"'%dq(batch))
for batch in wrap([[nm,"GigabitEthernet 0/0",[ip+"/24"]] for nm,ip,vn,st in servers],CHUNK):
    cmds.append('add ip_address_bulk "%s"'%dq(batch))

# 6b) Internet WayPoint shared segment
# Configuration: create an SVI (Vlan N) on the Internet WayPoint, and bind the SVI itself and
#       the Internet-side physical ports (port 0, port 1, ...) connected to each Svc device
#       into the same L2 segment (VlanIntSvc).
# The GE0/0 on the Svc-device side stays an L3 interface (unchanged).
if DO_SRV and svcs:
    inet_svi="Vlan %d"%inet_vlan
    # add an SVI to the Internet WayPoint
    cmds.append('add virtual_port_bulk "%s"'%dq([["Internet",[inet_svi]]]))
    # bind the SVI itself to an L2 segment (RULE: an SVI must always be bound via l2_segment_bulk)
    cmds.append('add l2_segment_bulk "%s"'%dq(
        [["Internet",inet_svi,[INET_VLAN_NAME]]]))
    # bind each Internet-side physical port (port N) to the same L2 segment
    inet_ports=[["Internet","port %d"%(inet_p_svc_start+i),[INET_VLAN_NAME]]
                for i in range(len(svcs))]
    for batch in wrap(inet_ports,CHUNK):
        cmds.append('add l2_segment_bulk "%s"'%dq(batch))

# 7) attributes (endpoints + waypoints)
for nm,ip,vn,st in servers: attr_rows.append([nm,("DEVICE",_role_color("Server")),"UCS C220 M6","Linux","Server"])
for nm,vn,st in pcs:        attr_rows.append([nm,("DEVICE",_role_color("PC")),"Workstation","Windows","PC"])
for nm,proto,port,fl in svcs: attr_rows.append([nm,("DEVICE",_role_color("Server")),"Internet Service","-","Server"])
attr_rows.append(["WAN",("WayPoint",_role_color("Cloud")),"","","Cloud"])
attr_rows.append(["Internet",("WayPoint",_role_color("Cloud")),"","","Cloud"])
_attr_hdr="['Device Name','Default','Model','OS','Stencil Type']"
for batch in wrap(attr_rows[1:],CHUNK):
    rows_str=','.join(_attr_row_str(r) for r in batch)
    cmds.append('rename attribute_bulk "[%s,%s]"'%(_attr_hdr,rows_str))

# ---------- write & summary ----------
with open(OUTFILE,"w",encoding="utf-8") as f: f.write("\n".join(cmds))

# out-of-scope IP CSV (candidate servers that were not adopted)
OOSFILE=os.path.join(OUTDIR,"out_of_scope_ips.csv")
reason_cnt=collections.Counter()
if DO_SRV:
    with open(OOSFILE,"w",newline="",encoding="utf-8") as f:
        w=csv.writer(f)
        w.writerow(["ip","region","reason","max_port_bytes","total_bytes","top_port","distinct_clients"])
        for rec in sorted(oos,key=lambda x:(x[2],-x[4])):
            w.writerow(rec); reason_cnt[rec[2]]+=1

# ---------- [FLOW] paste CSV (gen_flow_list.csv) : for pasting into [FLOW]test2.xlsx ----------
# Each row = one (Source/Dest device name, proto, service port) unit.
# Max.bandwidth(Mbps) = connection.transferBytes (received + sent total) * 8 / activeDuration (seconds)
#   If multiple flows of the same kind exist, the max Mbps is used. The Manual/Automatic routing columns are out of scope (left blank).
# Device names are output as the master-defined names (SRV_*/PC_*/Svc_*). Flows whose both ends cannot be resolved are excluded.
FLOWFILE=os.path.join(OUTDIR,"gen_flow_list.csv")
n_flow=0
if not args.no_flow:
    SERVNAME={20:"FTP-Data",21:"FTP",22:"SSH",23:"Telnet",25:"SMTP",53:"DNS",
        67:"DHCP",68:"DHCP",69:"TFTP",80:"HTTP",88:"Kerberos",110:"POP3",111:"RPC",
        123:"NTP",135:"MSRPC",137:"NetBIOS-NS",138:"NetBIOS-DGM",139:"NetBIOS-SSN",
        143:"IMAP",161:"SNMP",162:"SNMPTRAP",179:"BGP",389:"LDAP",443:"HTTPS",445:"SMB",
        465:"SMTPS",514:"Syslog",515:"LPD",587:"SMTP-Sub",636:"LDAPS",993:"IMAPS",
        995:"POP3S",1433:"MSSQL",1521:"Oracle",1645:"RADIUS",1646:"RADIUS",
        1812:"RADIUS",1813:"RADIUS-Acct",2049:"NFS",3268:"GC",3269:"GC-SSL",
        3306:"MySQL",3389:"RDP",5060:"SIP",5061:"SIP-TLS",5432:"PostgreSQL",
        5985:"WinRM",5986:"WinRM-S",8080:"HTTP-Alt",8443:"HTTPS-Alt",9100:"JetDirect"}
    def svc_label(port):
        nm=SERVNAME.get(port)
        return "%s(%d)"%(nm,port) if nm else str(port)
    def fmt_bw(x):                         # Mbps as a plain decimal (keep values < 1, avoid scientific notation)
        if x<=0: return "0"
        if x>=1: return ("%.2f"%x).rstrip("0").rstrip(".")
        d=min(max(2-int(math.floor(math.log10(x))),2),12)   # decimal places for ~3 significant figures
        return ("%.*f"%(d,x)).rstrip("0").rstrip(".") or "0"
    srv_name_by_ip={ip:nm for nm,ip,vn,st in servers}
    svc_name_by_pp={(proto,port):nm for nm,proto,port,fl in svcs}
    def dev_src(ip):                       # client side (initiator) -> master name
        if ip in srv_name_by_ip: return srv_name_by_ip[ip]
        if is_inside(ip): return pc_name_by_seg.get(k24(ip))
        return None
    def dev_dst(ip,proto,port):            # server side (destination) -> master name
        if ip in srv_name_by_ip: return srv_name_by_ip[ip]
        if not is_inside(ip): return svc_name_by_pp.get((proto,port))
        return None
    fmax=collections.defaultdict(float)    # (src,dst,proto,port) -> max Mbps
    with open(CSV,newline="",encoding="utf-8",errors="replace") as fh:
        r=csv.reader(fh); cols=next(r); ix={c:i for i,c in enumerate(cols)}
        Sip=ix["searchSubject.ipAddress"];Pip=ix["peer.ipAddress"]
        Pp=ix["peer.portProtocol.port"];PrS=ix["searchSubject.portProtocol.protocol"]
        Dur=ix["activeDuration"];By=ix["connection.transferBytes"]
        mxi=max(Sip,Pip,Pp,PrS,Dur,By)
        for v in r:
            if len(v)<=mxi: continue
            proto=(v[PrS] or "").upper()
            if proto not in ("TCP","UDP"): continue
            try: port=int(v[Pp])
            except: continue
            if port<=0: continue
            s=dev_src(v[Sip]); d=dev_dst(v[Pip],proto,port)
            if not s or not d or s==d: continue
            try: dur=float(v[Dur])/1000.0       # activeDuration is in milliseconds
            except: dur=0.0
            if dur<=0: continue                 # skip when duration is 0 (rate cannot be computed)
            try: by=float(v[By])
            except: by=0.0
            mbps=by*8.0/dur/1e6
            key=(s,d,proto,port)
            if mbps>fmax[key]: fmax[key]=mbps
    rows=sorted(fmax.items(),key=lambda kv:(-kv[1],kv[0][0],kv[0][1],kv[0][3]))
    with open(FLOWFILE,"w",newline="",encoding="utf-8") as f:
        w=csv.writer(f)
        w.writerow(["[Flow_List]"]+[""]*14)
        w.writerow(["No","Source Device Name","Destination Device Name","TCP/UDP/ICMP",
                    "Service name(Port)","Max. bandwidth(Mbps)",
                    "Manually rouging path settings","Automatic rouging path settings"]+[""]*7)
        for i,((s,d,proto,port),mbps) in enumerate(rows,1):
            w.writerow([i,s,d,proto,svc_label(port),fmt_bw(mbps)]+[""]*9)
    n_flow=len(rows)

print("===== AUTO-DETECTION (manual overrides take precedence) =====")
print("  inside_public : auto=%s  manual=%s  -> used=%s"
      %(sorted(auto_inside),list(MAN_INSIDE),list(INSIDE_PUBLIC)))
print("  dc_regions    : auto=%s  manual=%s  -> used=%s"
      %(sorted(auto_dc),sorted(MAN_DC),sorted(dc_regs)))
print("  spur_regions  : auto=%s  manual=%s  -> used=%s"
      %(sorted(auto_spur),sorted(MAN_SPUR),sorted(spur_regs)))
print("  seeds(hubs)   : %s   name_map(manual)=%s"%(seeds,NAME_MAP))
print("  site_cidrs    : %s  (dc=%s)"%(SITE_CIDRS,sorted(SITE_CIDR_DC)))
print("\n===== SITE GROUPING =====")
print("DC(server) sites [TOP row]:")
for s in dc_sites:
    print("  %-14s code=%-5s regions=%s subnets=%d"%(s,acode[s],site_regs[s],len(site_subnets[s])))
print("Client sites [BOTTOM row]:")
for s in client_sites:
    print("  %-14s code=%-5s regions=%s subnets=%d"%(s,acode[s],sorted(site_regs[s]),len(site_subnets[s])))
print("\n===== SERVER DETECTION (orientation-based) =====")
print("  config: server_min_bytes=%d require_tcp_synack=%s include_udp=%s subnet_min_flows=%d (min_flows=%d)"
      %(SRV_MIN_BYTES,REQ_SYNACK,INC_UDP,THRESH,MINF))
print("  candidate inside server IPs :", n_cand)
print("  adopted servers (1IP=1dev)  :", len(servers))
print("  out-of-scope IPs            :", len(oos),
      "(%s)"%", ".join("%s=%d"%(k,v) for k,v in reason_cnt.items()) if reason_cnt else "")
if DO_SRV: print("   -> out_of_scope_ips.csv :", OOSFILE)
print("\n===== ENDPOINTS (--endpoints %s) ====="%args.endpoints)
print("  servers(inside, 1IP=1dev):", len(servers))
print("  PCs(1 segment=1dev)      :", len(pcs))
print("  internet svc(proto,port) :", len(svcs), "(below-threshold skipped: %d)"%svc_oos)
if servers: print("   server sample:", [n for n,_,_,_ in servers[:5]])
if pcs:     print("   pc sample    :", [n for n,_,_ in pcs[:5]])
if svcs:    print("   svc sample   :", [(n,fl) for n,_,_,fl in svcs[:8]])
infra=len(alldev)-len(servers)-len(pcs)-len(svcs)
print("\nTotal adopted subnets:",len(adopted)," VLANs:101-%d"%(vlan-1))
print("Device count: infra=%d servers=%d pcs=%d svc=%d total=%d"%(infra,len(servers),len(pcs),len(svcs),len(alldev)))
print("Commands written:",len(cmds),"->",OUTFILE)
if not args.no_flow:
    print("Flow rows written:",n_flow,"->",FLOWFILE)
