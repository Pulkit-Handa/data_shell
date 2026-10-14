function el(tag, attrs={}, ...children) {
  const n = document.createElement(tag);
  Object.entries(attrs).forEach(([k,v]) => { if (k==='html') n.innerHTML=v; else n.setAttribute(k,v); });
  children.forEach(c => c && (typeof c==='string'?n.appendChild(document.createTextNode(c)):n.appendChild(c)));
  return n;
}

// Turn a form value into the right JSON type: numbers, booleans, and lists
// (e.g. ["USD","EUR"]) become typed values; anything else stays a string.
function coerceValue(raw) {
  const t = String(raw == null ? '' : raw).trim();
  if (t === '') return '';
  try { return JSON.parse(t); } catch (e) { /* not JSON */ }
  if (!isNaN(t)) return Number(t);
  return t;
}

const schemaRows = document.getElementById('schemaRows');
const addSchema = document.getElementById('addSchema');
const ruleRows = document.getElementById('ruleRows');
const addRule = document.getElementById('addRule');
const batchRows = document.getElementById('batchRows');
const addBatch = document.getElementById('addBatch');
const pgForm = document.getElementById('pgForm');
const pgOut = document.getElementById('pgOut');

function schemaRow() {
  const row = el('div', {class:'schema-row'}, 
    el('input',{name:'col_name',placeholder:'column name (e.g. amount)'}),
    el('select',{name:'col_type'},
      ...['Utf8','Int64','Float64','Boolean','Date','Datetime'].map(t=>el('option',{value:t},t))
    ),
    el('label',{}, el('input',{type:'checkbox',name:'nullable',checked:true}), ' nullable'),
    el('button',{type:'button',class:'btn ghost small'}, 'Remove')
  );
  row.querySelector('button').onclick = () => row.remove();
  schemaRows.appendChild(row);
}
function ruleRow() {
  const row = el('div',{class:'rule-row'},
    el('input',{name:'rule_id',placeholder:'rule_id (e.g. amount-positive)'}),
    el('select',{name:'check'},
      ...['NOT_NULL','GREATER_THAN','LESS_THAN','EQUAL','NOT_EQUAL','ALLOWED_VALUES','CONTAINS'].map(c=>el('option',{value:c},c))
    ),
    el('input',{name:'column',placeholder:'column'}),
    el('input',{name:'value',placeholder:'value (or ["USD","EUR"] for list)'}),
    el('input',{name:'description',placeholder:'description'}),
    el('button',{type:'button',class:'btn ghost small'}, 'Remove')
  );
  row.querySelector('button').onclick = () => row.remove();
  ruleRows.appendChild(row);
}
function batchRow() {
  const row = el('div',{class:'batch-row'},
    el('input',{name:'rule_id',placeholder:'rule_id'}),
    el('select',{name:'check'},
      ...['MIN_ROW_COUNT','MAX_ROW_COUNT','SUM_GREATER_THAN','AVG_GREATER_THAN'].map(c=>el('option',{value:c},c))
    ),
    el('input',{name:'value',placeholder:'value (number/json)'}),
    el('input',{name:'description',placeholder:'description'}),
    el('button',{type:'button',class:'btn ghost small'}, 'Remove')
  );
  row.querySelector('button').onclick = () => row.remove();
  batchRows.appendChild(row);
}

addSchema.onclick = schemaRow; addRule.onclick = ruleRow; addBatch.onclick = batchRow;
schemaRow(); ruleRow();

pgForm.onsubmit = async (e) => {
  e.preventDefault();
  const fd = new FormData(pgForm);
  const schema = Array.from(schemaRows.querySelectorAll('.schema-row')).map(r => ({
    name: r.querySelector('[name=col_name]').value.trim(),
    polars_type: r.querySelector('[name=col_type]').value,
    nullable: r.querySelector('[name=nullable]').checked
  })).filter(s=>s.name);
  const rules = Array.from(ruleRows.querySelectorAll('.rule-row')).map(r => ({
    rule_id: r.querySelector('[name=rule_id]').value.trim() || 'rule',
    check: r.querySelector('[name=check]').value,
    column: r.querySelector('[name=column]').value.trim(),
    value: coerceValue(r.querySelector('[name=value]').value),
    description: r.querySelector('[name=description]').value.trim() || undefined
  })).filter(r=>r.column);
  const batch_rules = Array.from(batchRows.querySelectorAll('.batch-row')).map(r => ({
    rule_id: r.querySelector('[name=rule_id]').value.trim() || 'batch',
    check: r.querySelector('[name=check]').value,
    value: coerceValue(r.querySelector('[name=value]').value),
    description: r.querySelector('[name=description]').value.trim() || undefined
  })).filter(r=>r.check && r.value!=='');

  const contract = {
    apiVersion: 'daas/v1', kind:'DataProduct',
    metadata:{name:'playground-contract',owner:'demo',version:'1.0.0',status:'ACTIVE',
      description:'Contract generated in the Data Shell playground'},
    spec:{
      sources:[{type:'LOCAL',connectionRef:'local',path:'source',format: (fd.get('source').name.endsWith('.csv')?'csv':'jsonl'), schema}],
      quality:{behavior:'DROP_ROW', rules, batch_rules},
      transformation:{map:[], reduce:[]},
      // Declared so the CLI's --target/--audit overrides apply to this run;
      // the server replaces these with a per-request temp location.
      delivery:{refreshSchedule:'0 * * * *',
        targets:[{type:'PARQUET',connectionRef:'playground',path:'curated/playground.parquet',mode:'overwrite'}],
        quarantinePath:'quarantine'}
    }
  };
  const cfd = new FormData();
  cfd.append('contract', new Blob([JSON.stringify(contract,null,2)],{type:'application/json'}), 'contract.json');
  cfd.append('source', fd.get('source'));
  cfd.append('tenant', fd.get('tenant')||'demo-tenant');
  const btn = pgForm.querySelector('button[type=submit]');
  btn.disabled=true; btn.textContent='Running...'; pgOut.textContent='Running enforcement...';
  try {
    const res = await fetch('/api/enforce', {method:'POST', body:cfd});
    const data = await res.json();
    pgOut.textContent = JSON.stringify(data,null,2);
  } catch(e){ pgOut.textContent='Error: '+e.message; }
  finally { btn.disabled=false; btn.textContent='Run Enforcement'; }
};
