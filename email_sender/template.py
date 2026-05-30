from datetime import date, datetime
from typing import List

from analysis.prop_analyzer import PropResult


def _render_brier_badge(brier_scores: dict) -> str:
    """Render a small Brier score badge for each sport.  Lower = better; 0.25 = coin-flip."""
    if not brier_scores:
        return ""
    parts = []
    for sport, score in sorted(brier_scores.items()):
        color = "#28a745" if score < 0.20 else "#e67e22" if score < 0.25 else "#dc3545"
        parts.append(
            f"<span style='margin-right:12px'>"
            f"<b>{sport}</b> Brier&nbsp;<span style='color:{color};font-weight:bold'>{score:.4f}</span>"
            f"</span>"
        )
    return (
        "<div style='margin-top:8px;font-size:11px;color:#555'>"
        "Model calibration (Brier score — lower is better; 0.25 = coin-flip): "
        + "".join(parts) + "</div>"
    )


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
        sport = r.get("sport", "")
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
        sport_tag = f"<span style='font-size:10px;color:#999;margin-left:4px'>{sport}</span>" if sport else ""
        rows_html += f"""
      <tr style="{row_style}">
        <td style="padding:6px 10px;font-weight:bold">{r.get('rank','')}</td>
        <td style="padding:6px 10px">{r.get('player_name','')}{sport_tag}
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
    {_render_brier_badge(cumulative.get("brier_scores", {}))}
  </div>"""


def render_email_html(
    results_by_sport: dict[str, list[PropResult]],
    run_date: date,
    duration_secs: float,
    yesterday_section_html: str = "",
) -> str:
    """
    results_by_sport: {"NBA": [...], "NHL": [...], ...} mapping sport name to sorted PropResult list.
    """
    # Flatten for global top-5 summary
    all_results = [r for sport_results in results_by_sport.values() for r in sport_results]
    all_results.sort(key=lambda r: r.hit_probability, reverse=True)

    high_conf = [r for r in all_results if r.hit_probability > 65]
    date_str = run_date.strftime("%B %d, %Y")
    top_5 = all_results[:5]

    top_5_html = "  ".join(
        f"<strong>{r.player_name}</strong> "
        f"<span style='color:{'#28a745' if r.direction=='OVER' else '#e67e22'}'>"
        f"{r.direction}</span> "
        f"{r.line} {r.stat_type} ({r.hit_probability:.0f}%)"
        for r in top_5
    )

    # Build per-sport table sections
    sport_sections_html = ""
    from config import SPORT_CONFIG
    for sport_name, sport_results in results_by_sport.items():
        if not sport_results:
            continue
        cfg = SPORT_CONFIG.get(sport_name, {})
        emoji = cfg.get("emoji", "")
        full_name = cfg.get("full_name", sport_name)

        sport_sections_html += f"""
  <div style="padding:10px 20px 4px;background:#2c3e7a;color:#fff;font-size:13px;font-weight:bold">
    {emoji} {full_name}
  </div>"""
        sport_sections_html += _render_picks_table(sport_results[:10])
        sport_sections_html += '<div style="margin-bottom:12px"></div>'

    total_props = len(all_results)
    n_sports = len([s for s, r in results_by_sport.items() if r])

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Prop Picks – {date_str}</title>
<style>
  body {{ margin: 0; padding: 0; background: #f0f2f5; font-family: Arial, Helvetica, sans-serif; font-size: 13px; }}
  .wrap {{ max-width: 980px; margin: 20px auto; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 12px rgba(0,0,0,.1); }}
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
    <h1>Multi-Sport Prop Picks &mdash; {date_str}</h1>
    <div class="meta">
      <span>&#127919; {total_props} Picks</span>
      <span>&#128994; {len(high_conf)} High Confidence (&gt;65%)</span>
      <span>&#127931; {n_sports} Sport{"s" if n_sports != 1 else ""} Active</span>
      <span>&#9201; {duration_secs:.0f}s runtime</span>
    </div>
  </div>

  <div class="topbox">
    <strong>Top 5 Today (All Sports):</strong>&nbsp;&nbsp;{top_5_html}
  </div>

  {yesterday_section_html}

  {sport_sections_html}

  <div class="footer">
    <p><strong>Methodology:</strong> Composite probability is built from a 20-game hit rate base, adjusted by:
    recent 5-game form (+20%), season average vs line (+15%), last 10-game average (+10%),
    H2H vs opponent <em>this season only</em> (+12%), opponent defensive rating (+10%),
    home/away split (+5%), rest days (+4%), pace factor (+4%).
    NBA data sourced from stats.nba.com (via nba_api).
    NHL data sourced from api-web.nhle.com. MLB data sourced from statsapi.mlb.com.
    Prop lines sourced exclusively from PrizePicks.
    H2H reflects current-season matchups only (zeroed if fewer than 2 games).
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


def _render_picks_table(picks: list) -> str:
    if not picks:
        return ""
    rows = "\n".join(_render_row(r) for r in picks)
    return f"""
  <table style="width:100%;border-collapse:collapse;margin-bottom:4px">
    <thead>
      <tr>
        <th>#</th><th>Player</th><th>Team</th><th>Direction</th>
        <th>Prop</th><th>Line</th><th>Predicted</th><th>Probability</th>
        <th>20G Hit Rate</th><th>H2H (this season)</th><th>Key Factors</th>
      </tr>
    </thead>
    <tbody>
{rows}
    </tbody>
  </table>"""


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


def render_plain_text(
    results_by_sport: dict[str, list[PropResult]],
    run_date: date,
    yesterday_results: list | None = None,
    cumulative_stats: dict | None = None,
    yesterday_date: date | None = None,
) -> str:
    lines = [
        f"Multi-Sport Prop Picks — {run_date.strftime('%B %d, %Y')}",
        "=" * 70,
        "",
    ]

    # Yesterday's results section
    if yesterday_results:
        evaluated = [r for r in yesterday_results if r.get("correct") in (0, 1)]
        if evaluated:
            n_correct = sum(1 for r in evaluated if r["correct"] == 1)
            n_total   = len(evaluated)
            pct_corr  = round(n_correct / n_total * 100) if n_total else 0
            pct_wrong = 100 - pct_corr
            date_label = yesterday_date.strftime("%B %d, %Y") if yesterday_date else "Yesterday"

            cum = cumulative_stats or {}
            cum_total   = cum.get("total_evaluated", 0)
            cum_correct = cum.get("total_correct", 0)
            cum_pct     = cum.get("accuracy_pct", 0.0)

            lines += [
                f"── YESTERDAY'S RESULTS — {date_label} ──",
                f"  {n_correct}/{n_total} correct ({pct_corr}% correct | {pct_wrong}% incorrect)"
                + (f"  |  All-time: {cum_correct}/{cum_total} ({cum_pct}%)" if cum_total else ""),
                "",
            ]

            correct_picks   = [r for r in evaluated if r["correct"] == 1]
            incorrect_picks = [r for r in evaluated if r["correct"] == 0]

            def _pick_line(r: dict) -> str:
                actual = r.get("actual_value")
                actual_str = f"{actual:.1f}" if actual is not None else "?"
                return (
                    f"    {r.get('player_name',''):<22} "
                    f"{'OVER ' if r.get('direction')=='OVER' else 'UNDER'} "
                    f"{r.get('stat_type',''):<14} "
                    f"Line:{r.get('line','')!s:<6}  Actual:{actual_str}  "
                    f"({r.get('hit_probability', 0):.0f}%)"
                )

            if correct_picks:
                lines.append(f"  CORRECT ({pct_corr}%):")
                lines += [_pick_line(r) for r in correct_picks]
                lines.append("")
            if incorrect_picks:
                lines.append(f"  INCORRECT ({pct_wrong}%):")
                lines += [_pick_line(r) for r in incorrect_picks]
                lines.append("")

    for sport_name, sport_results in results_by_sport.items():
        if not sport_results:
            continue
        lines.append(f"── {sport_name} — TOP 10 PICKS ──")
        for r in sport_results[:10]:
            dir_label = "OVER " if r.direction == "OVER" else "UNDER"
            lines.append(
                f"{r.rank:>3}. {r.player_name:<22} "
                f"{dir_label} {r.stat_type:<14} Line:{r.line:<6} "
                f"Prob:{r.hit_probability:.0f}%  "
                f"Pred:{r.predicted_value:.1f}"
            )
        lines.append("")
    lines += ["Data: stats.nba.com + nhle.com + mlb.com | Lines: PrizePicks",
              "Not financial advice."]
    return "\n".join(lines)
