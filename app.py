from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
import sqlite3, json, os
from datetime import datetime
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))
import confluence_sync as cf

app = Flask(__name__)
CORS(app)

DB_PATH = os.path.join(os.path.dirname(__file__), 'data', 'ri.db')

# ─── DB ───────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_db(); c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS projects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL, is_new INTEGER DEFAULT 1,
        doc_type TEXT, initiator TEXT,
        products TEXT DEFAULT '[]', risk TEXT DEFAULT 'Средний', stage TEXT,
        date_appeared TEXT, date_forecast TEXT, description TEXT,
        links TEXT DEFAULT '[]', notes TEXT DEFAULT '[]',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS actives (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL, doc_type TEXT, issued_by TEXT,
        products TEXT DEFAULT '[]',
        date_adopted TEXT, date_effective TEXT, description TEXT,
        links TEXT DEFAULT '[]', notes TEXT DEFAULT '[]',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    if c.execute('SELECT COUNT(*) FROM projects').fetchone()[0] == 0:
        seed_data(c); conn.commit()
    conn.close()

def row_to_dict(row):
    d = dict(row)
    for f in ['products','links','notes']:
        if f in d and isinstance(d[f], str):
            try: d[f] = json.loads(d[f])
            except: d[f] = []
    return d

def all_projects():
    conn = get_db()
    rows = conn.execute('SELECT * FROM projects ORDER BY created_at DESC').fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]

def all_actives():
    conn = get_db()
    rows = conn.execute('SELECT * FROM actives ORDER BY date_effective').fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]

def _notes_idx(items): return {str(i['id']): i.get('notes', []) for i in items}
def _links_idx(items): return {str(i['id']): i.get('links', []) for i in items}

def _full_sync():
    p = all_projects(); a = all_actives()
    cf.sync_all(p, a, _notes_idx(p), _notes_idx(a), _links_idx(p), _links_idx(a))

def _sync_proj(item):
    cf.sync_project(item, item.get('notes',[]), item.get('links',[]),
                    all_projects(), all_actives(), {},{},{},{})

def _sync_act(item):
    cf.sync_active(item, item.get('notes',[]), item.get('links',[]),
                   all_projects(), all_actives(), {},{},{},{})

# ─── SEED ─────────────────────────────────────────────────────
def seed_data(c):
    projects = [
        ('О потребительском кредите (изменения ПСК)', 1,
         'Законопроект (депутатский)', 'Группа депутатов ГД',
         json.dumps(['Кредитование','МФО']), 'Высокий', '1-е чтение',
         '2025-03-01', '2025-10-01',
         'Изменения в части полной стоимости кредита (ПСК), порядок расчёта и раскрытия информации для заёмщика.\n\nПоследствия: изменение расчётных моделей, доработка UI калькуляторов.',
         json.dumps([{'title':'Законопроект в СОЗД','url':'https://sozd.duma.gov.ru/bill/123456-8','type':'СОЗД Госдума'},{'title':'Пояснительная записка','url':'https://sozd.duma.gov.ru/docs','type':'Пояснительная записка'}]),
         json.dumps([{'date':'15.05.2025','text':'Принят в первом чтении.'},{'date':'10.04.2025','text':'Внесён в Госдуму.'}])),
        ('Информационная безопасность финансовых организаций', 0,
         'Положение ЦБ', 'Банк России',
         json.dumps(['Страхование','Кредитование']), 'Средний', 'Проект опубликован',
         '2025-04-20', '2025-09-15',
         'Новые требования к защите персональных данных клиентов. Обязательный аудит ИБ раз в год.',
         json.dumps([{'title':'Проект положения','url':'https://cbr.ru/project/456','type':'Сайт ЦБ'}]),
         json.dumps([{'date':'20.05.2025','text':'Проект на обсуждении до 01.07.2025.'}])),
        ('О требованиях к МФО (ужесточение)', 1,
         'Указание ЦБ', 'Банк России',
         json.dumps(['МФО']), 'Высокий', 'Обсуждение',
         '2025-04-15', '2025-07-20',
         'Ужесточение требований к капиталу МФО (минимум 100 млн руб). Ограничения на ставки — не более 0.8% в день.',
         json.dumps([{'title':'Проект указания','url':'https://cbr.ru/project/789','type':'Сайт ЦБ'}]),
         json.dumps([{'date':'28.05.2025','text':'Участие в рабочей группе ЦБ.'}])),
        ('О навязывании услуг при ОСАГО', 1,
         'Законопроект (правительственный)', 'Минфин',
         json.dumps(['ОСАГО','Страхование']), 'Высокий', 'ОРВ / regulation.gov.ru',
         '2025-04-28', '2025-12-01',
         'Запрет кросс-продаж при оформлении ОСАГО. Обязательное раскрытие права отказа от допуслуг.',
         json.dumps([{'title':'Проект на regulation.gov.ru','url':'https://regulation.gov.ru/p/777','type':'ОРВ / regulation.gov.ru'}]),
         json.dumps([{'date':'28.04.2025','text':'Срок обсуждения до 28.05.'}])),
        ('Единые требования к раскрытию информации о вкладах', 1,
         'Указание ЦБ', 'Банк России',
         json.dumps(['Вклады']), 'Средний', 'Проект опубликован',
         '2025-04-22', '2025-10-01',
         'Единая форма раскрытия информации о вкладах.',
         json.dumps([{'title':'Проект указания','url':'https://cbr.ru/project/555','type':'Сайт ЦБ'}]),
         json.dumps([{'date':'22.04.2025','text':'Проект опубликован.'}])),
        ('О регулировании кредитных историй', 0,
         'Положение ЦБ', 'Банк России',
         json.dumps(['Кредитование']), 'Средний', 'Обсуждение',
         '2025-03-15', '2025-08-15',
         'Расширение перечня данных, передаваемых в БКИ.',
         json.dumps([{'title':'Проект','url':'https://cbr.ru/project/987','type':'Сайт ЦБ'}]),
         json.dumps([])),
        ('Стандарты ОСАГО (новая редакция)', 0,
         'Приказ ФОИВ', 'РСА',
         json.dumps(['ОСАГО']), 'Низкий', 'Разработка',
         '2025-05-10', '2025-12-01',
         'Обновление стандартов урегулирования убытков.',
         json.dumps([]), json.dumps([{'date':'10.05.2025','text':'Опубликована концепция.'}])),
    ]
    for p in projects:
        c.execute('INSERT INTO projects (title,is_new,doc_type,initiator,products,risk,stage,date_appeared,date_forecast,description,links,notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', p)

    actives = [
        ('О персональных данных (поправки к 152-ФЗ)', 'Федеральный закон', 'Государственная Дума',
         json.dumps(['Кредитование','МФО','Страхование','Вклады','ОСАГО']),
         '2025-04-01', '2025-06-01',
         'Новые требования к согласиям на обработку данных. Право на удаление.',
         json.dumps([{'title':'Текст закона','url':'https://publication.pravo.gov.ru/321-FZ','type':'Официальный текст'}]),
         json.dumps([{'date':'01.04.2025','text':'Закон подписан.'}])),
        ('Новые требования к идентификации клиентов МФО', 'Указание ЦБ', 'Банк России',
         json.dumps(['МФО']), '2025-03-20', '2025-07-01',
         'Обязательная биометрическая идентификация при выдаче займов свыше 15 000 рублей.',
         json.dumps([{'title':'Указание ЦБ','url':'https://cbr.ru/act/890','type':'Официальный текст'}]),
         json.dumps([{'date':'20.03.2025','text':'Зарегистрировано в Минюсте.'}])),
        ('Изменения в расчёте КБМ по ОСАГО', 'Указание ЦБ', 'Банк России',
         json.dumps(['ОСАГО']), '2025-02-15', '2025-09-01',
         'Новая методика расчёта коэффициента бонус-малус.',
         json.dumps([]), json.dumps([])),
        ('О страховых агентах (обновление требований)', 'Постановление Правительства', 'Правительство РФ',
         json.dumps(['Страхование','ОСАГО']), '2025-04-10', '2025-10-01',
         'Новые квалификационные требования к страховым агентам.',
         json.dumps([]), json.dumps([])),
    ]
    for a in actives:
        c.execute('INSERT INTO actives (title,doc_type,issued_by,products,date_adopted,date_effective,description,links,notes) VALUES (?,?,?,?,?,?,?,?,?)', a)


# ─── ROUTES ───────────────────────────────────────────────────

@app.route('/')
def index(): return render_template('index.html')

@app.route('/api/status')
def status():
    return jsonify({'confluence_enabled': cf.ENABLED,
                    'confluence_url': cf.CONFLUENCE_URL if cf.ENABLED else None})

# Projects
@app.route('/api/projects', methods=['GET'])
def get_projects(): return jsonify(all_projects())

@app.route('/api/projects', methods=['POST'])
def create_project():
    data = request.json; conn = get_db(); c = conn.cursor()
    c.execute('INSERT INTO projects (title,is_new,doc_type,initiator,products,risk,stage,date_appeared,date_forecast,description,links,notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', (
        data.get('title',''), 1, data.get('doc_type',''), data.get('initiator',''),
        json.dumps(data.get('products',[])), data.get('risk','Средний'), data.get('stage',''),
        data.get('date_appeared',''), data.get('date_forecast',''), data.get('description',''),
        json.dumps(data.get('links',[])),
        json.dumps([{'date': datetime.now().strftime('%d.%m.%Y'), 'text': 'Проект добавлен в мониторинг.'}])
    ))
    nid = c.lastrowid; conn.commit()
    item = row_to_dict(conn.execute('SELECT * FROM projects WHERE id=?', (nid,)).fetchone())
    conn.close(); _sync_proj(item)
    return jsonify(item), 201

@app.route('/api/projects/<int:pid>', methods=['PUT'])
def update_project(pid):
    data = request.json; conn = get_db()
    conn.execute('UPDATE projects SET title=?,doc_type=?,initiator=?,products=?,risk=?,stage=?,date_appeared=?,date_forecast=?,description=?,links=?,is_new=0,updated_at=CURRENT_TIMESTAMP WHERE id=?', (
        data.get('title',''), data.get('doc_type',''), data.get('initiator',''),
        json.dumps(data.get('products',[])), data.get('risk','Средний'), data.get('stage',''),
        data.get('date_appeared',''), data.get('date_forecast',''), data.get('description',''),
        json.dumps(data.get('links',[])), pid
    ))
    conn.commit()
    item = row_to_dict(conn.execute('SELECT * FROM projects WHERE id=?', (pid,)).fetchone())
    conn.close(); _sync_proj(item)
    return jsonify(item)

@app.route('/api/projects/<int:pid>', methods=['DELETE'])
def delete_project(pid):
    conn = get_db()
    row = conn.execute('SELECT title FROM projects WHERE id=?', (pid,)).fetchone()
    title = row['title'] if row else ''
    conn.execute('DELETE FROM projects WHERE id=?', (pid,)); conn.commit(); conn.close()
    cf.delete_project_page(title); _full_sync()
    return jsonify({'ok': True})

@app.route('/api/projects/<int:pid>/notes', methods=['POST'])
def add_project_note(pid):
    data = request.json; conn = get_db()
    row = conn.execute('SELECT * FROM projects WHERE id=?', (pid,)).fetchone()
    if not row: conn.close(); return jsonify({'error': 'Not found'}), 404
    item = row_to_dict(row); notes = item['notes']
    note = {'date': datetime.now().strftime('%d.%m.%Y'), 'text': data.get('text','').strip()}
    notes.insert(0, note)
    conn.execute('UPDATE projects SET notes=?,updated_at=CURRENT_TIMESTAMP WHERE id=?', (json.dumps(notes), pid))
    conn.commit(); conn.close(); item['notes'] = notes; _sync_proj(item)
    return jsonify(note), 201

# Actives
@app.route('/api/actives', methods=['GET'])
def get_actives(): return jsonify(all_actives())

@app.route('/api/actives', methods=['POST'])
def create_active():
    data = request.json; conn = get_db(); c = conn.cursor()
    c.execute('INSERT INTO actives (title,doc_type,issued_by,products,date_adopted,date_effective,description,links,notes) VALUES (?,?,?,?,?,?,?,?,?)', (
        data.get('title',''), data.get('doc_type',''), data.get('issued_by',''),
        json.dumps(data.get('products',[])), data.get('date_adopted',''), data.get('date_effective',''),
        data.get('description',''), json.dumps(data.get('links',[])),
        json.dumps([{'date': datetime.now().strftime('%d.%m.%Y'), 'text': 'Акт добавлен в базу.'}])
    ))
    nid = c.lastrowid; conn.commit()
    item = row_to_dict(conn.execute('SELECT * FROM actives WHERE id=?', (nid,)).fetchone())
    conn.close(); _sync_act(item)
    return jsonify(item), 201

@app.route('/api/actives/<int:aid>', methods=['PUT'])
def update_active(aid):
    data = request.json; conn = get_db()
    conn.execute('UPDATE actives SET title=?,doc_type=?,issued_by=?,products=?,date_adopted=?,date_effective=?,description=?,links=?,updated_at=CURRENT_TIMESTAMP WHERE id=?', (
        data.get('title',''), data.get('doc_type',''), data.get('issued_by',''),
        json.dumps(data.get('products',[])), data.get('date_adopted',''), data.get('date_effective',''),
        data.get('description',''), json.dumps(data.get('links',[])), aid
    ))
    conn.commit()
    item = row_to_dict(conn.execute('SELECT * FROM actives WHERE id=?', (aid,)).fetchone())
    conn.close(); _sync_act(item)
    return jsonify(item)

@app.route('/api/actives/<int:aid>', methods=['DELETE'])
def delete_active(aid):
    conn = get_db()
    row = conn.execute('SELECT title FROM actives WHERE id=?', (aid,)).fetchone()
    title = row['title'] if row else ''
    conn.execute('DELETE FROM actives WHERE id=?', (aid,)); conn.commit(); conn.close()
    cf.delete_active_page(title); _full_sync()
    return jsonify({'ok': True})

@app.route('/api/actives/<int:aid>/notes', methods=['POST'])
def add_active_note(aid):
    data = request.json; conn = get_db()
    row = conn.execute('SELECT * FROM actives WHERE id=?', (aid,)).fetchone()
    if not row: conn.close(); return jsonify({'error': 'Not found'}), 404
    item = row_to_dict(row); notes = item['notes']
    note = {'date': datetime.now().strftime('%d.%m.%Y'), 'text': data.get('text','').strip()}
    notes.insert(0, note)
    conn.execute('UPDATE actives SET notes=?,updated_at=CURRENT_TIMESTAMP WHERE id=?', (json.dumps(notes), aid))
    conn.commit(); conn.close(); item['notes'] = notes; _sync_act(item)
    return jsonify(note), 201

@app.route('/api/sync', methods=['POST'])
def force_sync():
    if not cf.ENABLED: return jsonify({'error': 'Confluence не настроен'}), 400
    try: _full_sync(); return jsonify({'ok': True})
    except Exception as e: return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    init_db()
    import socket
    try: local_ip = socket.gethostbyname(socket.gethostname())
    except: local_ip = '127.0.0.1'

    print('\n' + '='*55)
    print('  Regulatory Intelligence — запущен!')
    print('='*55)
    print(f'  Локально:   http://localhost:5000')
    print(f'  В сети:     http://{local_ip}:5000')
    if cf.ENABLED:
        print(f'  Confluence: ✅ {cf.CONFLUENCE_URL}  [{cf.CONFLUENCE_SPACE}]')
        print('  Начальная синхронизация…')
    else:
        print('  Confluence: ⚠️  не настроен — заполните файл .env')
    print('  Остановка: Ctrl+C')
    print('='*55 + '\n')

    if cf.ENABLED:
        try: _full_sync(); print('[Confluence] ✅ Готово\n')
        except Exception as e: print(f'[Confluence] ⚠️  {e}\n')

    app.run(host='0.0.0.0', port=5000, debug=False)
