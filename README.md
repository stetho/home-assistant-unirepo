# home-assistant-unirepo
A home for all my home assistant scripts. 

# ha-audit
I built this when I was moving Home Assistant from an old PC to a new PC and I didn't want to use the backup which was full of years of unused entities. 

After you've cloned the repo, cd into it and 

```
export HA_TOKEN="a long-lived access token"
export HA_URL="http://192.168.8.2:8123"
```

Then 

```
python3 -m venv .venv
source .venv/bin/activate
pip install websockets
```



```
python ha_audit.py
```

This will create two files 

audit.json - a list of everything

rebuild.md - a MarkDown file formatted as a decision list

If you already have your new Home Assistant machine set up and you've been moving things to it you can 

```
export HA_TOKEN="a long-lived access token from your new server"
export HA_URL="http://192.168.8.3:8123"

mv audit.json old-audit.json
mv rebuild.md old-rebuild.md

python ha_audit.py

python ha_audit.py compare old-audit.json audit.json
```

which will give you progress.md showing you what already exists on both.
