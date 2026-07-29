import requests
import json

urls = [
    "https://api.llama.fi/summary/dexs/pump.fun",
    "https://api.llama.fi/summary/fees/pump.fun?dataType=dailyFees",
    "https://api.llama.fi/summary/fees/pump.fun?dataType=dailyRevenue"
]

for url in urls:
    try:
        print(f"Fetching {url}...")
        r = requests.get(url)
        print(f"Status: {r.status_code}")
        data = r.json()
        print("Keys:", list(data.keys()))
        # Print a small sample of totalDataChart or similar structure
        for key in ['totalDataChart', 'totalFees', 'totalRevenue', 'dailyFees', 'dailyRevenue', 'totalVolume', 'dailyVolume']:
            if key in data:
                print(f"  {key} type: {type(data[key])}")
                if isinstance(data[key], list) and len(data[key]) > 0:
                    print(f"  Sample {key}[0]: {data[key][0]}")
                    print(f"  Sample {key}[-1]: {data[key][-1]}")
    except Exception as e:
        print(f"Error fetching {url}: {e}")
