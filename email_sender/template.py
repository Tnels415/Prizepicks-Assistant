from datetime import date, datetime
from typing import List

from analysis.prop_analyzer import PropResult


def render_yesterday_section(
    yesterday_results: list,
    cumulative: dict,
    yesterday_date: date,
) -> str:
    if not yesterday_results:
        return ""

    evaluated = [r for r in yesterday_results if r.get("correct") in (0, 1)]
    dnp = [r for r in yesterday_results if r.get("correct") == -1]

    if not evaluated:
        return ""

    n_correct = sum(1 for r in evaluated if r["correct"] == 1)
    n_total = len(evaluated)
    pct = round(n_correct / n_total * 100) if n_total else 0

    cum_total = cumulative.get("total_evaluated", 0)
    cum_correct = cumulative.get("total_correct", 0)
    cum_pct = cumulative.get("accuracy_pct", 0.0)

    score_color = "#28a745" if pct >= 60 else "#e67e22" if pct >= 50 else "#dc3545"

    rows_html = ""
    for r in sorted(yesterday_results, key=lambda x: x.get("rank", 99))[:10]:
        correct = r.get("correct")
        actual = r.get("actual_value")
        if correct == -1:
            icon = "&#8212;"
            row_style = "background:#f8f9fa"
            actual_str = "DNP"
        elif correct == 1:
            icon = "&#10003;"
            row_style = "background:#d4f0da"
            actual_str = f"{actual:.1f}" if actual is not None else "?"
        elif correct == 0:
            icon = "&#10007;"
            row_style = "background:#fde8e8"
            actual_str = f"{actual:.1f}" if actual is not None else "?"
        else:
            icon = "&#8230;"
            row_style = ""
            actual_str = "—"

        dir_color = "#28a745" if r.get("direction") == "OVER" else "#e67e22"
        rows_html += f"""
      <tr style="{row_style}">
        <td style="padding:6px 10px;font-weight:bold">{r.get('rank','')}</td>
        <td style="padding:6px 10px">{r.get('player_name','')}
          <span style="color:{dir_color};font-weight:bold;font-size:11px"> {r.get('direction','')}</span></td>
        <td style="padding:6px 10px">{r.get('stat_type','')}</td>
        <td style="padding:6px 10px;font-weight:bold">{r.get('line','')}</td>
        <td style="padding:6px 10px">{r.get('predicted_value','')}</td>
        <td style="padding:6px 10px">{r.get('hit_probability',''):.0f}%</td>
        <td style="padding:6px 10px;font-weight:bold">{actual_str}</td>
        <td style="padding:6px 10px;font-size:16px;text-align:center">{icon}</td>
      </tr>"""

    stat_accuracy = ""
    for stat, data in cumulative.get("by_stat_type", {}).items():
        stat_accuracy += f"<span style='margin-right:14px'><b>{stat}</b> {data['pct']}%</span>"

    return f"""
  <div style="padding:14px 20px;background:#fff8e1;border-left:4px solid #f0a500;margin-bottom:0">
    <div style="font-weight:bold;font-size:14px;color:#333;margin-bottom:6px">
      Yesterday's Results &mdash; {yesterday_date.strftime('%B %d, %Y')}
    </div>
    <div style="font-size:13px;margin-bottom:10px">
      <span style="font-size:20px;font-weight:bold;color:{score_color}">{n_correct}/{n_total}</span>
      <span style="color:#555;margin-left:6px">correct ({pct}% yesterday)</span>
      &nbsp;&nbsp;|&nbsp;&nbsp;
      <span style="color:#555">All-time: <b>{cum_correct}/{cum_total}</b> ({cum_pct}%)</span>
      {f'&nbsp;&nbsp;|&nbsp;&nbsp;<span style="color:#999;font-size:11px">{len(dnp)} DNP excluded</span>' if dnp else ''}
    </div>
    <table style="width:100%;border-collapse:collapse;font-size:12px">
      <tr style="background:#f0a500;color:#fff">
        <th style="padding:5px 10px;text-align:left">#</th>
        <th style="padding:5px 10px;text-align:left">Player</th>
        <th style="padding:5px 10px;text-align:left">Prop</th>
        <th style="padding:5px 10px;text-align:left">Line</th>
        <th style="padding:5px 10px;text-align:left">Predicted</th>
        <th style="padding:5px 10px;text-align:left">Prob</th>
        <th style="padding:5px 10px;text-align:left">Actual</th>
        <th style="padding:5px 10px;text-align:center">Result</th>
      </tr>
      {rows_html}
    </table>
    {f'<div style="margin-top:8px;font-size:11px;color:#777">Accuracy by stat type: {stat_accuracy}</div>' if stat_accuracy else ''}
  </div>"""


def render_email_html(
    results: List[PropResult],
    run_date: date,
    duration_secs: float,
    yesterday_section_html: str = "",
) -> str:
    high_conf = [r for r in results if r.hit_probability > 65]
    n_games = len({r.opponent_team_abbr for r in results})
    date_str = run_date.strftime("%B %d, %Y")
    top_5 = results[:5]

    top_5_html = "  ".join(
        f"<strong>{r.player_name}</strong> "
        f"<span style='color:{'#28a745' if r.direction=='OVER' else '#e67e22'}'>"
        f"{r.direction}</span> "
        f"{r.line} {r.stat_type} ({r.hit_probability:.0f}%)"
        for r in top_5
    )

    rows_html = "\n".join(_render_row(r) for r in results)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NBA Prop Picks – {date_str}</title>
<style>
  body {{ margin: 0; padding: 0; background: #f0f2f5; font-family: Arial, Helvetica, sans-serif; font-size: 13px; }}
  .wrap {{ max-width: 960px; margin: 20px auto; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 12px rgba(0,0,0,.1); }}
  .header {{ background: #1a2a5e; color: #fff; padding: 22px 26px; }}
  .header h1 {{ margin: 0 0 6px; font-size: 22px; letter-spacing: .5px; }}
  .header .meta {{ font-size: 12px; opacity: .8; }}
  .header .meta span {{ margin-right: 18px; }}
  .topbox {{ background: #eef2ff; border-left: 4px solid #1a2a5e; padding: 12px 20px; font-size: 12.5px; line-height: 1.8; }}
  .topbox strong {{ color: #1a2a5e; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th {{ background: #2c3e7a; color: #fff; padding: 9px 10px; text-align: left; font-size: 12px; white-space: nowrap; }}
  td {{ padding: 8px 10px; vertical-align: top; border-bottom: 1px solid #eee; }}
  tr:nth-child(even) td {{ background: #f8f9fb; }}
  .high {{ background: #d4f0da !important; }}
  .med  {{ background: #fff9e6 !important; }}
  .low  {{ background: #f8f9fa !important; }}
  .badge {{ display: inline-block; border-radius: 12px; padding: 3px 9px; font-weight: bold; font-size: 13px; }}
  .badge.high {{ background: #28a745; color: #fff; }}
  .badge.med  {{ background: #ffc107; color: #333; }}
  .badge.low  {{ background: #adb5bd; color: #fff; }}
  .dir-over  {{ color: #28a745; font-weight: bold; }}
  .dir-under {{ color: #e67e22; font-weight: bold; }}
  .pred-up   {{ color: #28a745; font-weight: bold; }}
  .pred-down {{ color: #dc3545; font-weight: bold; }}
  .pill {{ display: inline-block; background: #e9ecef; border-radius: 6px; padding: 1px 7px; font-size: 10.5px; margin: 1px 2px; white-space: nowrap; }}
  .dq-badge {{ display: inline-block; background: #ffe08a; border-radius: 4px; padding: 1px 5px; font-size: 10px; color: #856404; margin-left: 4px; }}
  .footer {{ background: #f0f2f5; padding: 14px 20px; font-size: 10.5px; color: #888; line-height: 1.6; }}
</style>
</head>
<body>
<div class="wrap">

  <div class="header">
    <h1>NBA Prop Picks &mdash; {date_str}</h1>
    <div class="meta">
      <span>&#127936; {len(results)} Directions Ranked</span>
      <span>&#128994; {len(high_conf)} High Confidence (&gt;65%)</span>
      <span>&#127944; ~{n_games} Games Today</span>
      <span>&#9201; {duration_secs:.0f}s runtime</span>
    </div>
  </div>

  <div class="topbox">
    <strong>Top 5 Today:</strong>&nbsp;&nbsp;{top_5_html}
  </div>

  {yesterday_section_html}

  <table>
    <thead>
      <tr>
        <th>#</th>
        <th>Player</th>
        <th>Team</th>
        <th>Direction</th>
        <th>Prop</th>
        <th>Line</th>
        <th>Predicted</th>
        <th>Probability</th>
        <th>20G Hit Rate</th>
        <th>H2H (this season)</th>
        <th>Key Factors</th>
      </tr>
    </thead>
    <tbody>
{rows_html}
    </tbody>
  </table>

  <div class="footer">
    <p><strong>Methodology:</strong> Composite probability is built from a 20-game hit rate base, adjusted by:
    recent 5-game form (+20%), season average vs line (+15%), last 10-game average (+10%),
    H2H vs opponent <em>this season only</em> (+12%), opponent defensive rating (+10%),
    home/away split (+5%), rest days (+4%), pace factor (+4%).
    Data sourced from stats.nba.com (via nba_api) and PrizePicks public API.
    H2H stats reflect current-season matchups only and are zeroed out if fewer than 2 games available.
    </p>
    <p>Generated {datetime.now().strftime("%Y-%m-%d %H:%M:%S")} &nbsp;|&nbsp;
    <em>For entertainment purposes only. Not financial advice.</em></p>
  </div>

</div>
</body>
</html>"""


def _conf_class(prob: float) -> str:
    if prob > 65:
        return "high"
    if prob >= 55:
        return "med"
    return "low"


def _render_row(r: PropResult) -> str:
    cc = _conf_class(r.hit_probability)
    dir_cls = "dir-over" if r.direction == "OVER" else "dir-under"
    pred_cls = "pred-up" if r.predicted_value > r.line else "pred-down"

    h2h_text = "N/A"
    if r.h2h_sample_size >= 2:
        hits = round(r.h2h_hit_rate * r.h2h_sample_size)
        h2h_text = f"{hits}/{r.h2h_sample_size} vs {r.opponent_team_abbr}"

    pills = "".join(f'<span class="pill">{f}</span>' for f in r.key_factors)

    dq_badge = ""
    if r.data_quality == "partial":
        dq_badge = '<span class="dq-badge">partial data</span>'
    elif r.data_quality == "minimal":
        dq_badge = '<span class="dq-badge">minimal data</span>'

    return f"""      <tr class="{cc}">
        <td><strong>{r.rank}</strong></td>
        <td><strong>{r.player_name}</strong><br><span style="font-size:11px;color:#888">{r.position}</span></td>
        <td>{r.team_abbr}</td>
        <td><span class="{dir_cls}">{r.direction}</span></td>
        <td>{r.stat_type}</td>
        <td style="font-weight:bold">{r.line}</td>
        <td class="{pred_cls}">{r.predicted_value:.1f}</td>
        <td><span class="badge {cc}">{r.hit_probability:.0f}%</span></td>
        <td>{r.hit_rate_20:.0%} ({r.games_analyzed}G)</td>
        <td style="font-size:11.5px">{h2h_text}</td>
        <td>{pills}{dq_badge}</td>
      </tr>"""


def render_plain_text(results: List[PropResult], run_date: date) -> str:
    lines = [
        f"NBA Prop Picks — {run_date.strftime('%B %d, %Y')}",
        "=" * 70,
        "",
    ]
    for r in results[:30]:
        lines.append(
            f"{r.rank:>3}. {r.player_name:<22} {r.direction:<5} "
            f"{r.stat_type:<12} Line:{r.line:<6} "
            f"Prob:{r.hit_probability:.0f}%  "
            f"Pred:{r.predicted_value:.1f}"
        )
    lines += ["", "Data: stats.nba.com + PrizePicks public API",
              "Not financial advice."]
    return "\n".join(lines)
