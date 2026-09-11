import json

with open('scratch_leaderboard.json', encoding='utf-8') as f:
    d = json.load(f)

for row in d.get('boards', {}).get('static', []):
    print(row.get('rank'), row.get('name'), row.get('roll_id'), 'err:', row.get('err_rate_overall'), 'feed:', row.get('feed_percent'), 'rps:', row.get('peak_rps'), 'reqs:', row.get('total_requests'), 'ms:', row.get('mean_response_ms'))
