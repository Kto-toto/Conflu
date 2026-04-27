"""
confluence_sync.py — Синхронизация с Confluence Cloud
"""

import requests
import os
import json
from datetime import datetime

CONFLUENCE_URL       = os.getenv('CONFLUENCE_URL', '').rstrip('/')
CONFLUENCE_EMAIL     = os.getenv('CONFLUENCE_EMAIL', '')
CONFLUENCE_TOKEN     = os.getenv('CONFLUENCE_TOKEN', '')
CONFLUENCE_SPACE     = os.getenv('CONFLUENCE_SPACE', '')
CONFLUENCE_PARENT_ID = os.getenv('CONFLUENCE_PARENT_ID', '')

ENABLED = bool(CONFLUENCE_URL and CONFLUENCE_EMAIL and CONFLUENCE_TOKEN and CONFLUENCE_SPACE)

# ── Справочники ──────────────────────────────────────────────
PROD_BG = {
    'Кредитование': ('#dbeafe', '#1d4ed8'),
    'МФО':          ('#fef3c7', '#b45309'),
    'Вклады':       ('#ede9fe', '#6d28d9'),
    'ОСАГО':        ('#fee2e2', '#b91c1c'),
    'Страхование':  ('#dcfce7', '#15803d'),
}

RISK_BG = {
    'Высокий': ('#fee2e2', '#991b1b'),
    'Средний':  ('#fef3c7', '#92400e'),
    'Низкий':  ('#dcfce7', '#166534'),
}

STAGE_CHAINS = {
    'Законопроект (депутатский)':
        ['Инициатива', 'Внесён в ГД', '1-е чтение', '2-е чтение', '3-е чтение', 'Принят ГД', 'Одобрен СФ', 'Подписан'],
    'Законопроект (правительственный)':
        ['ОРВ', 'Внесён в ГД', '1-е чтение', '2-е чтение', '3-е чтение', 'Принят ГД', 'Одобрен СФ', 'Подписан'],
    'Постановление Правительства': ['Разработка', 'ОРВ', 'Принято'],
    'Распоряжение Правительства':  ['Разработка', 'ОРВ', 'Принято'],
    'Приказ ФОИВ':  ['Разработка', 'ОРВ', 'Подписан', 'Рег. в Минюсте'],
    'Указание ЦБ':  ['Обсуждение', 'Проект опубл.', 'Утверждён', 'Рег. в Минюсте'],
    'Положение ЦБ': ['Обсуждение', 'Проект опубл.', 'Утверждён', 'Рег. в Минюсте'],
}

# ════════════════════════════════════════════════════════════
#  API
# ════════════════════════════════════════════════════════════

def _auth():    return (CONFLUENCE_EMAIL, CONFLUENCE_TOKEN)
def _hdrs():    return {'Content-Type': 'application/json', 'Accept': 'application/json'}

def _api(method, path, **kw):
    url = f"{CONFLUENCE_URL}/wiki/rest/api{path}"
    r = requests.request(method, url, auth=_auth(), headers=_hdrs(), timeout=15, **kw)
    r.raise_for_status()
    return r.json() if r.content else {}

def _find(title):
    try:
        d = _api('GET', f'/content?spaceKey={CONFLUENCE_SPACE}&title={requests.utils.quote(title)}&expand=version')
        res = d.get('results', [])
        return res[0] if res else None
    except Exception:
        return None

def _create(title, body, parent_id=None):
    payload = {'type': 'page', 'title': title,
               'space': {'key': CONFLUENCE_SPACE},
               'body': {'storage': {'value': body, 'representation': 'storage'}}}
    pid = parent_id or CONFLUENCE_PARENT_ID
    if pid:
        payload['ancestors'] = [{'id': str(pid)}]
    return _api('POST', '/content', json=payload)

def _update(page_id, title, body, version):
    return _api('PUT', f'/content/{page_id}', json={
        'type': 'page', 'title': title,
        'version': {'number': version + 1},
        'body': {'storage': {'value': body, 'representation': 'storage'}}
    })

def _upsert(title, body, parent_id=None):
    ex = _find(title)
    if ex:
        return _update(ex['id'], title, body, ex['version']['number'])
    return _create(title, body, parent_id)

def get_or_create_section(title):
    p = _find(title)
    if p:
        return p['id']
    created = _create(title, f'<h1>{title}</h1>', CONFLUENCE_PARENT_ID or None)
    return created['id']

# ════════════════════════════════════════════════════════════
#  Визуальные хелперы — всё в виде «наклеек» (pill-badges)
# ════════════════════════════════════════════════════════════

def _fmt(val):
    if not val:
        return '—'
    if isinstance(val, str) and len(val) == 10:
        try:
            return datetime.strptime(val, '%Y-%m-%d').strftime('%d.%m.%Y')
        except Exception:
            pass
    return str(val)

def _days(val):
    if not val:
        return 9999
    try:
        d = datetime.strptime(val, '%Y-%m-%d') if isinstance(val, str) else val
        return (d - datetime.now()).days
    except Exception:
        return 9999

# Базовая «наклейка» — pill с цветным фоном
def _pill(text, bg, fg, bold=False):
    fw = 'font-weight:600;' if bold else 'font-weight:500;'
    return (f'<span style="display:inline-block;background:{bg};color:{fg};'
            f'padding:3px 10px;border-radius:12px;font-size:11px;{fw}'
            f'white-space:nowrap;margin:2px 3px 2px 0;">{text}</span>')

def _risk_pill(risk):
    bg, fg = RISK_BG.get(risk, ('#f1f5f9', '#475569'))
    return _pill(risk, bg, fg, bold=True)

def _stage_pill(stage):
    return _pill(stage, '#f1f5f9', '#475569')

def _prod_pills(products):
    if not products:
        return '—'
    if isinstance(products, str):
        products = [p.strip() for p in products.split(',') if p.strip()]
    parts = []
    for p in products:
        bg, fg = PROD_BG.get(p, ('#f1f5f9', '#374151'))
        parts.append(_pill(p, bg, fg))
    return ' '.join(parts)  # пробел между наклейками

# Пайплайн стадий: каждая стадия — отдельная наклейка
def _stage_pipeline(doc_type, current_stage):
    # Нормализуем: убираем длинный суффикс "/ regulation.gov.ru" для сравнения
    chains = STAGE_CHAINS.get(doc_type, [])
    if not chains:
        return _stage_pill(current_stage or '—')

    # Ищем совпадение (частичное, т.к. в DB может быть полное название)
    ci = -1
    for idx, s in enumerate(chains):
        if current_stage and (s in current_stage or current_stage in s):
            ci = idx
            break

    parts = []
    for i, s in enumerate(chains):
        if i == ci:
            p = _pill(s, '#2563eb', '#ffffff', bold=True)
        elif i < ci:
            p = _pill(s, '#dcfce7', '#166534')
        else:
            p = _pill(s, '#f1f5f9', '#94a3b8')
        parts.append(p)
    return ' '.join(parts)  # пробелы между этапами

# Заголовок-баннер страницы (без служебных сообщений)
def _page_header(icon, title, subtitle=''):
    s = (f'<table style="width:100%;border-collapse:collapse;margin-bottom:20px;">'
         f'<tr><td style="background:#0f172a;padding:16px 20px;border-radius:8px;">'
         f'<div style="font-size:20px;font-weight:700;color:#ffffff;letter-spacing:-.01em;">'
         f'{icon} {title}</div>')
    if subtitle:
        s += f'<div style="font-size:12px;color:#64748b;margin-top:4px;">{subtitle}</div>'
    s += '</td></tr></table>'
    return s

# Карточка-метрика для дашборда
def _metric_card(icon, label, value, sub, bg, fg):
    return (f'<td style="width:25%;padding:16px 20px;background:{bg};border-radius:8px;'
            f'vertical-align:top;border:1px solid rgba(0,0,0,.04);">'
            f'<div style="font-size:11px;color:{fg};font-weight:600;opacity:.8;">{icon} {label}</div>'
            f'<div style="font-size:34px;font-weight:700;color:{fg};margin:8px 0 4px;line-height:1;">{value}</div>'
            f'<div style="font-size:11px;color:{fg};opacity:.6;">{sub}</div></td>')

# Секция-заголовок внутри страницы
def _section_h(text):
    return (f'<div style="background:#1e293b;color:#94a3b8;padding:7px 14px;'
            f'border-radius:6px;font-size:11px;font-weight:700;text-transform:uppercase;'
            f'letter-spacing:.06em;margin:20px 0 10px;">{text}</div>')

# ════════════════════════════════════════════════════════════
#  Дашборд
# ════════════════════════════════════════════════════════════

def _build_dashboard_html(projects, actives):
    total   = len(projects)
    high    = sum(1 for p in projects if p.get('risk') == 'Высокий')
    soon    = sum(1 for p in projects if 0 <= _days(p.get('date_forecast')) <= 60)
    new_cnt = sum(1 for p in projects if p.get('is_new'))

    upcoming = sorted(
        [a for a in actives if _days(a.get('date_effective')) > 0],
        key=lambda a: a.get('date_effective', '9999')
    )[:5]

    html = _page_header('⚖️', 'Regulatory Intelligence — Дашборд',
                        f'Обновлено: {datetime.now().strftime("%d.%m.%Y %H:%M")}')

    # Метрики
    html += '<table style="width:100%;border-collapse:collapse;border-spacing:8px;margin-bottom:24px;"><tr>'
    html += _metric_card('📄', 'Всего проектов', total, 'в мониторинге', '#f8fafc', '#334155')
    html += _metric_card('🔴', 'Высокий риск', high, 'требуют внимания', '#fff1f2', '#991b1b')
    html += _metric_card('⏰', 'Прогноз ≤ 60 дней', soon, 'ближайшие по срокам', '#fffbeb', '#92400e')
    html += _metric_card('✨', 'Новых', new_cnt, 'недавно добавлены', '#eff6ff', '#1e40af')
    html += '</tr></table>'

    # Таблица проектов
    html += _section_h(f'Проекты нормативных актов — {total}')
    html += ('<table style="width:100%;border-collapse:collapse;font-size:13px;">'
             '<tr style="background:#1e293b;color:#e2e8f0;">'
             '<th style="padding:9px 12px;text-align:left;font-weight:600;">Проект</th>'
             '<th style="padding:9px 12px;text-align:left;font-weight:600;">Продукты</th>'
             '<th style="padding:9px 12px;text-align:center;font-weight:600;">Риск</th>'
             '<th style="padding:9px 12px;text-align:left;font-weight:600;">Стадия</th>'
             '<th style="padding:9px 12px;text-align:center;font-weight:600;">Дата / Прогноз</th>'
             '</tr>')

    for i, p in enumerate(projects):
        bg   = '#ffffff' if i % 2 == 0 else '#f8fafc'
        d    = _days(p.get('date_forecast'))
        dfg  = '#dc2626' if 0 <= d <= 60 else '#374151'
        dbold= 'font-weight:600;' if 0 <= d <= 60 else ''
        new  = (' ' + _pill('NEW', '#2563eb', '#ffffff', bold=True)) if p.get('is_new') else ''
        ct   = f'Карточка: {p["title"]}'
        html += (f'<tr style="background:{bg};border-bottom:1px solid #e8e8e3;">'
                 f'<td style="padding:10px 12px;">'
                 f'<ac:link><ri:page ri:content-title="{ct}" ri:space-key="{CONFLUENCE_SPACE}"/>'
                 f'<ac:plain-text-link-body><![CDATA[{p["title"]}]]></ac:plain-text-link-body></ac:link>'
                 f'{new}<br><span style="font-size:10px;color:#94a3b8;">{p.get("doc_type","")}</span></td>'
                 f'<td style="padding:10px 12px;">{_prod_pills(p.get("products"))}</td>'
                 f'<td style="padding:10px 12px;text-align:center;">{_risk_pill(p.get("risk",""))}</td>'
                 f'<td style="padding:10px 12px;">{_stage_pill(p.get("stage",""))}</td>'
                 f'<td style="padding:10px 12px;text-align:center;color:{dfg};{dbold}">{_fmt(p.get("date_forecast"))}</td>'
                 f'</tr>')
    html += '</table>'

    # Ближайшие вступления в силу
    if upcoming:
        html += _section_h('Ближайшие вступления в силу')
        html += ('<table style="width:100%;border-collapse:collapse;font-size:13px;">'
                 '<tr style="background:#14532d;color:#dcfce7;">'
                 '<th style="padding:9px 12px;text-align:left;">Акт</th>'
                 '<th style="padding:9px 12px;text-align:left;">Продукты</th>'
                 '<th style="padding:9px 12px;text-align:center;">Дата принятия</th>'
                 '<th style="padding:9px 12px;text-align:center;">Вступает в силу</th>'
                 '</tr>')
        for i, a in enumerate(upcoming):
            bg  = '#ffffff' if i % 2 == 0 else '#f8fafc'
            d   = _days(a.get('date_effective'))
            fg  = '#dc2626' if d <= 30 else '#374151'
            bld = 'font-weight:600;' if d <= 30 else ''
            ct  = f'НПА: {a["title"]}'
            html += (f'<tr style="background:{bg};border-bottom:1px solid #e8e8e3;">'
                     f'<td style="padding:10px 12px;font-weight:500;">'
                     f'<ac:link><ri:page ri:content-title="{ct}" ri:space-key="{CONFLUENCE_SPACE}"/>'
                     f'<ac:plain-text-link-body><![CDATA[{a["title"]}]]></ac:plain-text-link-body></ac:link>'
                     f'<br><span style="font-size:10px;color:#94a3b8;">{a.get("doc_type","")}</span></td>'
                     f'<td style="padding:10px 12px;">{_prod_pills(a.get("products"))}</td>'
                     f'<td style="padding:10px 12px;text-align:center;color:#374151;">{_fmt(a.get("date_adopted"))}</td>'
                     f'<td style="padding:10px 12px;text-align:center;color:{fg};{bld}">{_fmt(a.get("date_effective"))}</td>'
                     f'</tr>')
        html += '</table>'

    return html

# ════════════════════════════════════════════════════════════
#  Реестр проектов
# ════════════════════════════════════════════════════════════

def _build_projects_html(projects):
    html = _page_header('📋', 'Реестр проектов НПА',
                        f'Всего: {len(projects)} · Обновлено: {datetime.now().strftime("%d.%m.%Y %H:%M")}')
    html += ('<table style="width:100%;border-collapse:collapse;font-size:13px;">'
             '<tr style="background:#1e293b;color:#e2e8f0;">'
             '<th style="padding:9px 12px;text-align:left;">Проект</th>'
             '<th style="padding:9px 12px;text-align:left;">Продукты</th>'
             '<th style="padding:9px 12px;text-align:center;">Риск</th>'
             '<th style="padding:9px 12px;text-align:left;">Стадия</th>'
             '<th style="padding:9px 12px;text-align:center;">Появился</th>'
             '<th style="padding:9px 12px;text-align:center;">Прогноз принятия</th>'
             '</tr>')
    for i, p in enumerate(projects):
        bg   = '#ffffff' if i % 2 == 0 else '#f8fafc'
        d    = _days(p.get('date_forecast'))
        dfg  = '#dc2626' if 0 <= d <= 60 else '#374151'
        dbold= 'font-weight:600;' if 0 <= d <= 60 else ''
        new  = (' ' + _pill('NEW', '#2563eb', '#ffffff', bold=True)) if p.get('is_new') else ''
        ct   = f'Карточка: {p["title"]}'
        html += (f'<tr style="background:{bg};border-bottom:1px solid #e8e8e3;">'
                 f'<td style="padding:10px 12px;">'
                 f'<ac:link><ri:page ri:content-title="{ct}" ri:space-key="{CONFLUENCE_SPACE}"/>'
                 f'<ac:plain-text-link-body><![CDATA[{p["title"]}]]></ac:plain-text-link-body></ac:link>'
                 f'{new}<br>'
                 f'<span style="font-size:10px;color:#94a3b8;">{p.get("doc_type","")} · {p.get("initiator","")}</span></td>'
                 f'<td style="padding:10px 12px;">{_prod_pills(p.get("products"))}</td>'
                 f'<td style="padding:10px 12px;text-align:center;">{_risk_pill(p.get("risk",""))}</td>'
                 f'<td style="padding:10px 12px;">{_stage_pill(p.get("stage",""))}</td>'
                 f'<td style="padding:10px 12px;text-align:center;color:#374151;">{_fmt(p.get("date_appeared"))}</td>'
                 f'<td style="padding:10px 12px;text-align:center;color:{dfg};{dbold}">{_fmt(p.get("date_forecast"))}</td>'
                 f'</tr>')
    html += '</table>'
    return html

# ════════════════════════════════════════════════════════════
#  Реестр вступивших в силу НПА  (было: "Действующие НПА")
# ════════════════════════════════════════════════════════════

def _build_actives_html(actives):
    sorted_a = sorted(actives, key=lambda a: a.get('date_effective') or '9999')
    html = _page_header('✅', 'НПА, вступившие в силу',
                        f'Всего: {len(actives)} · Обновлено: {datetime.now().strftime("%d.%m.%Y %H:%M")}')
    html += ('<table style="width:100%;border-collapse:collapse;font-size:13px;">'
             '<tr style="background:#14532d;color:#dcfce7;">'
             '<th style="padding:9px 12px;text-align:left;">Акт</th>'
             '<th style="padding:9px 12px;text-align:left;">Продукты</th>'
             '<th style="padding:9px 12px;text-align:center;">Дата принятия</th>'
             '<th style="padding:9px 12px;text-align:center;">Вступает в силу</th>'
             '</tr>')
    for i, a in enumerate(sorted_a):
        bg  = '#ffffff' if i % 2 == 0 else '#f8fafc'
        d   = _days(a.get('date_effective'))
        dfg = '#dc2626' if 0 < d <= 30 else ('#94a3b8' if d < 0 else '#374151')
        bld = 'font-weight:600;' if 0 < d <= 30 else ''
        ct  = f'НПА: {a["title"]}'
        html += (f'<tr style="background:{bg};border-bottom:1px solid #e8e8e3;">'
                 f'<td style="padding:10px 12px;font-weight:500;">'
                 f'<ac:link><ri:page ri:content-title="{ct}" ri:space-key="{CONFLUENCE_SPACE}"/>'
                 f'<ac:plain-text-link-body><![CDATA[{a["title"]}]]></ac:plain-text-link-body></ac:link>'
                 f'<br><span style="font-size:10px;color:#94a3b8;">{a.get("doc_type","")} · {a.get("issued_by","")}</span></td>'
                 f'<td style="padding:10px 12px;">{_prod_pills(a.get("products"))}</td>'
                 f'<td style="padding:10px 12px;text-align:center;">{_fmt(a.get("date_adopted"))}</td>'
                 f'<td style="padding:10px 12px;text-align:center;color:{dfg};{bld}">{_fmt(a.get("date_effective"))}</td>'
                 f'</tr>')
    html += '</table>'
    return html

# ════════════════════════════════════════════════════════════
#  Календарь
# ════════════════════════════════════════════════════════════

def _build_calendar_html(projects, actives):
    events = []
    for p in projects:
        if p.get('date_forecast'):
            events.append({'date': p['date_forecast'], 'title': p['title'], 'type': 'project',
                           'risk': p.get('risk',''), 'products': p.get('products',[]),
                           'extra': p.get('stage',''), 'id': p['id']})
    for a in actives:
        if a.get('date_effective'):
            events.append({'date': a['date_effective'], 'title': a['title'], 'type': 'active',
                           'products': a.get('products',[]), 'extra': 'Вступает в силу', 'id': a['id']})
    events.sort(key=lambda e: e['date'])

    html = _page_header('📅', 'Календарь изменений',
                        f'Обновлено: {datetime.now().strftime("%d.%m.%Y %H:%M")}')

    # Легенда
    html += (f'<div style="display:flex;gap:12px;margin-bottom:16px;flex-wrap:wrap;">'
             f'{_pill("Проект — прогнозная дата принятия", "#dbeafe", "#1d4ed8")}'
             f'{_pill("Вступил в силу — точная дата", "#ede9fe", "#6d28d9")}'
             f'{_pill("⚠ Ближайшие 14 дней — красным", "#fee2e2", "#991b1b")}'
             f'</div>')

    html += ('<table style="width:100%;border-collapse:collapse;font-size:13px;">'
             '<tr style="background:#3b0764;color:#e9d5ff;">'
             '<th style="padding:9px 12px;text-align:center;width:100px;">Дата</th>'
             '<th style="padding:9px 12px;text-align:center;width:100px;">Тип</th>'
             '<th style="padding:9px 12px;text-align:left;">Название</th>'
             '<th style="padding:9px 12px;text-align:left;">Продукты</th>'
             '<th style="padding:9px 12px;text-align:left;">Стадия</th>'
             '<th style="padding:9px 12px;text-align:center;width:80px;">Риск</th>'
             '</tr>')

    last_month = ''
    month_names = ['','Январь','Февраль','Март','Апрель','Май','Июнь',
                   'Июль','Август','Сентябрь','Октябрь','Ноябрь','Декабрь']

    for i, e in enumerate(events):
        try:
            d = datetime.strptime(e['date'], '%Y-%m-%d')
            mk = f"{d.year}-{d.month:02d}"
            if mk != last_month:
                html += (f'<tr><td colspan="6" style="background:#1e293b;color:#64748b;'
                         f'padding:6px 12px;font-size:11px;font-weight:700;text-align:center;'
                         f'letter-spacing:.05em;text-transform:uppercase;">'
                         f'{month_names[d.month]} {d.year}</td></tr>')
                last_month = mk
        except Exception:
            pass

        days   = _days(e['date'])
        isUrg  = 0 <= days <= 14
        isPast = days < 0
        bg     = '#fff1f2' if isUrg else ('#f8fafc' if isPast else ('#ffffff' if i % 2 == 0 else '#f8fafc'))
        dfg    = '#dc2626' if isUrg else ('#94a3b8' if isPast else '#374151')
        dbold  = 'font-weight:700;' if isUrg else ''
        is_p   = e['type'] == 'project'
        t_pill = _pill('Проект', '#dbeafe', '#1d4ed8') if is_p else _pill('Вступил в силу', '#ede9fe', '#6d28d9')
        cp     = 'Карточка: ' if is_p else 'НПА: '
        ct     = cp + e['title']

        html += (f'<tr style="background:{bg};border-bottom:1px solid #e8e8e3;">'
                 f'<td style="padding:9px 12px;text-align:center;color:{dfg};{dbold};font-variant-numeric:tabular-nums;">{_fmt(e["date"])}</td>'
                 f'<td style="padding:9px 12px;text-align:center;">{t_pill}</td>'
                 f'<td style="padding:9px 12px;font-weight:500;">'
                 f'<ac:link><ri:page ri:content-title="{ct}" ri:space-key="{CONFLUENCE_SPACE}"/>'
                 f'<ac:plain-text-link-body><![CDATA[{e["title"]}]]></ac:plain-text-link-body></ac:link></td>'
                 f'<td style="padding:9px 12px;">{_prod_pills(e.get("products"))}</td>'
                 f'<td style="padding:9px 12px;">{_stage_pill(e.get("extra","")) if e.get("extra") else "—"}</td>'
                 f'<td style="padding:9px 12px;text-align:center;">{_risk_pill(e["risk"]) if e.get("risk") else "—"}</td>'
                 f'</tr>')
    html += '</table>'
    return html

# ════════════════════════════════════════════════════════════
#  Карточка: Проект НПА
# ════════════════════════════════════════════════════════════

def _build_project_card_html(project, notes, links):
    days  = _days(project.get('date_forecast'))
    dfg   = '#dc2626' if 0 <= days <= 60 else '#374151'
    dbold = 'font-weight:600;' if 0 <= days <= 60 else ''

    html = _page_header('📄', project.get('title',''),
                        project.get('doc_type','') + (' · ' + project.get('initiator','') if project.get('initiator') else ''))

    # Двухколоночная раскладка: свойства | описание
    html += '<table style="width:100%;border-collapse:collapse;margin-bottom:24px;"><tr>'

    # Левая: свойства
    html += '<td style="width:44%;vertical-align:top;padding-right:18px;">'
    fields = [
        ('Продукты',         _prod_pills(project.get('products'))),
        ('Риск',             _risk_pill(project.get('risk','—'))),
        ('Тип документа',    _pill(project.get('doc_type','—'), '#f1f5f9', '#374151') if project.get('doc_type') else '—'),
        ('Инициатор',        project.get('initiator','—')),
        ('Стадия',           _stage_pipeline(project.get('doc_type',''), project.get('stage',''))),
        ('Дата появления',   f'<span style="font-variant-numeric:tabular-nums;">{_fmt(project.get("date_appeared"))}</span>'),
        ('Прогноз принятия', f'<span style="font-variant-numeric:tabular-nums;color:{dfg};{dbold}">{_fmt(project.get("date_forecast"))}</span>'),
    ]
    html += '<table style="width:100%;border-collapse:collapse;font-size:13px;">'
    for k, v in fields:
        html += (f'<tr style="border-bottom:1px solid #f0f0ed;">'
                 f'<td style="padding:7px 0 7px 0;font-size:10px;color:#a1a1aa;'
                 f'font-weight:600;text-transform:uppercase;letter-spacing:.04em;width:38%;'
                 f'white-space:nowrap;vertical-align:top;padding-right:10px;">{k}</td>'
                 f'<td style="padding:7px 0;">{v}</td></tr>')
    html += '</table></td>'

    # Правая: описание
    desc = (project.get('description') or '').replace('\n', '<br>')
    html += (f'<td style="vertical-align:top;background:#f8fafc;border-radius:8px;'
             f'padding:16px;border-left:3px solid #2563eb;">'
             f'<div style="font-size:10px;color:#64748b;font-weight:700;text-transform:uppercase;'
             f'letter-spacing:.05em;margin-bottom:10px;">Суть изменений</div>'
             f'<div style="font-size:13px;line-height:1.7;color:#18181b;">'
             f'{desc or "<em style=\'color:#a1a1aa;\'>Описание не добавлено</em>"}</div>'
             f'</td></tr></table>')

    # Источники
    html += _section_h('🔗 Источники и документы')
    if links:
        html += '<table style="width:100%;border-collapse:collapse;font-size:13px;">'
        for lnk in links:
            url   = lnk.get('url') or lnk.get('URL','')
            name  = lnk.get('title') or lnk.get('Название','')
            ltype = lnk.get('type') or lnk.get('Тип','')
            html += (f'<tr style="border-bottom:1px solid #f0f0ed;">'
                     f'<td style="padding:9px 0;width:30%;">{_pill(ltype, "#eff6ff", "#1d4ed8")}</td>'
                     f'<td style="padding:9px 6px;font-weight:500;">'
                     f'<a href="{url}" style="color:#2563eb;">{name}</a></td>'
                     f'<td style="padding:9px 0;font-size:10px;color:#94a3b8;">{url}</td>'
                     f'</tr>')
        html += '</table>'
    else:
        html += '<p style="color:#a1a1aa;font-size:13px;font-style:italic;">Ссылки не добавлены</p>'

    # Заметки
    html += _section_h('📝 Рабочие заметки')
    if notes:
        for n in notes:
            date = n.get('date') or n.get('Дата','')
            text = n.get('text') or n.get('Текст заметки','')
            html += (f'<div style="border-left:3px solid #2563eb;padding:10px 14px;'
                     f'margin-bottom:8px;background:#f8fafc;border-radius:0 6px 6px 0;">'
                     f'<div style="font-size:10px;color:#94a3b8;margin-bottom:4px;'
                     f'font-variant-numeric:tabular-nums;">{date}</div>'
                     f'<div style="font-size:13px;color:#18181b;line-height:1.5;">{text}</div></div>')
    else:
        html += '<p style="color:#a1a1aa;font-size:13px;font-style:italic;">Заметок пока нет</p>'

    return html

# ════════════════════════════════════════════════════════════
#  Карточка: НПА, вступивший в силу  (было: "Действующий НПА")
# ════════════════════════════════════════════════════════════

def _build_active_card_html(active, notes, links):
    days  = _days(active.get('date_effective'))
    dfg   = '#dc2626' if 0 < days <= 30 else '#374151'
    dbold = 'font-weight:600;' if 0 < days <= 30 else ''

    html = _page_header('✅', active.get('title',''),
                        active.get('doc_type','') + (' · ' + active.get('issued_by','') if active.get('issued_by') else ''))

    html += '<table style="width:100%;border-collapse:collapse;margin-bottom:24px;"><tr>'

    html += '<td style="width:44%;vertical-align:top;padding-right:18px;">'
    fields = [
        ('Продукты',          _prod_pills(active.get('products'))),
        ('Тип документа',     _pill(active.get('doc_type','—'), '#f1f5f9', '#374151') if active.get('doc_type') else '—'),
        ('Принявший орган',   active.get('issued_by','—')),
        ('Дата принятия',     f'<span style="font-variant-numeric:tabular-nums;">{_fmt(active.get("date_adopted"))}</span>'),
        ('Вступает в силу',   f'<span style="font-variant-numeric:tabular-nums;color:{dfg};{dbold}">{_fmt(active.get("date_effective"))}</span>'),
    ]
    html += '<table style="width:100%;border-collapse:collapse;font-size:13px;">'
    for k, v in fields:
        html += (f'<tr style="border-bottom:1px solid #f0f0ed;">'
                 f'<td style="padding:7px 0;font-size:10px;color:#a1a1aa;'
                 f'font-weight:600;text-transform:uppercase;letter-spacing:.04em;width:38%;'
                 f'white-space:nowrap;vertical-align:top;padding-right:10px;">{k}</td>'
                 f'<td style="padding:7px 0;">{v}</td></tr>')
    html += '</table></td>'

    desc = (active.get('description') or '').replace('\n', '<br>')
    html += (f'<td style="vertical-align:top;background:#f0fdf4;border-radius:8px;'
             f'padding:16px;border-left:3px solid #16a34a;">'
             f'<div style="font-size:10px;color:#64748b;font-weight:700;text-transform:uppercase;'
             f'letter-spacing:.05em;margin-bottom:10px;">Суть изменений</div>'
             f'<div style="font-size:13px;line-height:1.7;color:#18181b;">'
             f'{desc or "<em style=\'color:#a1a1aa;\'>Описание не добавлено</em>"}</div>'
             f'</td></tr></table>')

    html += _section_h('🔗 Источники и документы')
    if links:
        html += '<table style="width:100%;border-collapse:collapse;font-size:13px;">'
        for lnk in links:
            url   = lnk.get('url') or lnk.get('URL','')
            name  = lnk.get('title') or lnk.get('Название','')
            ltype = lnk.get('type') or lnk.get('Тип','')
            html += (f'<tr style="border-bottom:1px solid #f0f0ed;">'
                     f'<td style="padding:9px 0;width:30%;">{_pill(ltype, "#eff6ff", "#1d4ed8")}</td>'
                     f'<td style="padding:9px 6px;font-weight:500;">'
                     f'<a href="{url}" style="color:#2563eb;">{name}</a></td>'
                     f'<td style="padding:9px 0;font-size:10px;color:#94a3b8;">{url}</td>'
                     f'</tr>')
        html += '</table>'
    else:
        html += '<p style="color:#a1a1aa;font-size:13px;font-style:italic;">Ссылки не добавлены</p>'

    html += _section_h('📝 Рабочие заметки')
    if notes:
        for n in notes:
            date = n.get('date') or n.get('Дата','')
            text = n.get('text') or n.get('Текст заметки','')
            html += (f'<div style="border-left:3px solid #16a34a;padding:10px 14px;'
                     f'margin-bottom:8px;background:#f0fdf4;border-radius:0 6px 6px 0;">'
                     f'<div style="font-size:10px;color:#94a3b8;margin-bottom:4px;">{date}</div>'
                     f'<div style="font-size:13px;color:#18181b;line-height:1.5;">{text}</div></div>')
    else:
        html += '<p style="color:#a1a1aa;font-size:13px;font-style:italic;">Заметок пока нет</p>'

    return html

# ════════════════════════════════════════════════════════════
#  Публичные функции синхронизации
# ════════════════════════════════════════════════════════════

def sync_all(projects, actives, notes_by_proj, notes_by_act, links_by_proj, links_by_act):
    if not ENABLED:
        return
    try:
        _sync_all_pages(projects, actives, notes_by_proj, notes_by_act, links_by_proj, links_by_act)
    except Exception as e:
        print(f'[Confluence] Ошибка полной синхронизации: {e}')

def sync_project(project, notes, links, all_projects, all_actives, *args):
    if not ENABLED:
        return
    try:
        cid  = get_or_create_section('📁 Карточки проектов')
        _upsert(f'Карточка: {project["title"]}', _build_project_card_html(project, notes, links), cid)
        _sync_summaries(all_projects, all_actives)
    except Exception as e:
        print(f'[Confluence] Ошибка синхр. проекта #{project.get("id")}: {e}')

def sync_active(active, notes, links, all_projects, all_actives, *args):
    if not ENABLED:
        return
    try:
        aid  = get_or_create_section('📁 НПА, вступившие в силу')
        _upsert(f'НПА: {active["title"]}', _build_active_card_html(active, notes, links), aid)
        _sync_summaries(all_projects, all_actives)
    except Exception as e:
        print(f'[Confluence] Ошибка синхр. НПА #{active.get("id")}: {e}')

def delete_project_page(title):
    if not ENABLED:
        return
    try:
        p = _find(f'Карточка: {title}')
        if p:
            _api('DELETE', f'/content/{p["id"]}')
    except Exception as e:
        print(f'[Confluence] Ошибка удаления карточки проекта: {e}')

def delete_active_page(title):
    if not ENABLED:
        return
    try:
        p = _find(f'НПА: {title}')
        if p:
            _api('DELETE', f'/content/{p["id"]}')
    except Exception as e:
        print(f'[Confluence] Ошибка удаления карточки НПА: {e}')

def _sync_summaries(all_projects, all_actives):
    root = CONFLUENCE_PARENT_ID or None
    _upsert('🏠 Дашборд — Regulatory Intelligence',
            _build_dashboard_html(all_projects, all_actives), root)
    _upsert('📋 Реестр проектов НПА',
            _build_projects_html(all_projects), root)
    _upsert('✅ НПА, вступившие в силу',
            _build_actives_html(all_actives), root)
    _upsert('📅 Календарь изменений',
            _build_calendar_html(all_projects, all_actives), root)

def _sync_all_pages(projects, actives, notes_by_proj, notes_by_act, links_by_proj, links_by_act):
    _sync_summaries(projects, actives)
    cid = get_or_create_section('📁 Карточки проектов')
    for p in projects:
        notes = notes_by_proj.get(str(p['id']), [])
        links = links_by_proj.get(str(p['id']), [])
        _upsert(f'Карточка: {p["title"]}', _build_project_card_html(p, notes, links), cid)
    aid = get_or_create_section('📁 НПА, вступившие в силу')
    for a in actives:
        notes = notes_by_act.get(str(a['id']), [])
        links = links_by_act.get(str(a['id']), [])
        _upsert(f'НПА: {a["title"]}', _build_active_card_html(a, notes, links), aid)
    print(f'[Confluence] ✅ Синхронизация: {len(projects)} проектов, {len(actives)} НПА')
