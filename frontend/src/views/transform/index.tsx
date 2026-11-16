import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/transform'
const CONTRACT_ENDPOINT = '/api/transform/contracts/summary'
const columns = [
  "模板编号",
  "关联服务",
  "输入字段",
  "输出字段",
  "格式转换",
  "默认填充",
  "数组展开",
  "嵌套打平"
]
const actions = [
  "提交校验",
  "启用模板",
  "停用模板"
]
const statuses = [
  "草稿",
  "待校验",
  "已启用",
  "已停用"
]
const stats = [
  {
    "label": "转换模板总数",
    "value": 0
  },
  {
    "label": "已启用模板",
    "value": 0
  },
  {
    "label": "待校验模板",
    "value": 0
  }
]

type Row = Record<string, any>

export default function TransformView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [contracts, setContracts] = useState<Row[]>([])
  const [showContracts, setShowContracts] = useState(false)
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('转换模板列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '响应转换列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function loadContracts() {
    setShowContracts((prev) => !prev)
    try {
      const response = await request(CONTRACT_ENDPOINT)
      if (!response.ok) throw new Error('编排步骤契约读取失败')
      const payload = await response.json()
      setContracts(payload.items ?? [])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '编排步骤契约读取失败')
    }
  }

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      if (!response.ok) {
        throw new Error('响应转换动作未生效，请稍后重试')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '响应转换操作失败')
    }
  }

  return (
    <section className="page" data-module="transform">
      <header className="page-head">
        <div>
          <h2>响应转换管理</h2>
          <p className="page-desc">维护响应转换模板，并只读消费查询编排写入的编排步骤契约，按同一执行顺序与字段清单做转换。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => void loadContracts()}>
            {showContracts ? '收起编排步骤契约' : '查看编排步骤契约（只读）'}
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出响应转换清单
          </button>
        </div>
      </header>

      {showContracts && (
        <div className="drawer">
          <div className="drawer-head">
            <h3>编排步骤契约 · 转换模块只读消费（步骤顺序与字段清单以查询编排为准）</h3>
          </div>
          <table className="data-table compact">
            <thead>
              <tr>
                <th>流程编号</th><th>契约版本</th><th>来源批次</th><th>执行顺序</th>
                <th>拓扑波次</th><th>并行度</th><th>字段清单</th><th>合并行数</th><th>写入团队</th>
              </tr>
            </thead>
            <tbody>
              {contracts.map((contract) => (
                <tr key={String(contract.id)}>
                  <td>{contract['流程编号']}</td>
                  <td>v{contract['契约版本']}</td>
                  <td>#{contract['来源执行批次']}</td>
                  <td>{(contract['执行顺序'] ?? []).join(' → ')}</td>
                  <td>{JSON.stringify(contract['拓扑波次'])}</td>
                  <td>{contract['并行度']}</td>
                  <td>
                    <details>
                      <summary>{(contract['字段清单'] ?? []).length} 个字段</summary>
                      <ul className="field-list">
                        {(contract['字段清单'] ?? []).map((field: Row) => (
                          <li key={field['字段']}>
                            <strong>{field['字段']}</strong> ← {field['采纳来源步骤']}
                            <span className="page-desc">（{field['判定依据']}）</span>
                          </li>
                        ))}
                      </ul>
                    </details>
                  </td>
                  <td>{contract['合并行数']}</td>
                  <td>{contract['写入团队']}</td>
                </tr>
              ))}
              {contracts.length === 0 && (
                <tr><td className="empty-state" colSpan={9}>暂无契约：查询编排执行成功后才会写入</td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无响应转换数据，可先登记转换模板</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条响应转换记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
