import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/flow'
const columns = [
  "流程编号",
  "流程名称",
  "步骤序列",
  "数据依赖",
  "失败回退",
  "超时秒数",
  "并行度",
  "结果合并"
]
const actions = [
  "提交校验",
  "启用流程",
  "停用流程"
]
const statuses = [
  "草稿",
  "待校验",
  "已启用",
  "已停用"
]
const stats = [
  {
    "label": "编排流程总数",
    "value": 0
  },
  {
    "label": "已启用流程",
    "value": 0
  },
  {
    "label": "待校验流程",
    "value": 0
  }
]

// 只有归属团队可以执行编排、调整步骤顺序；其他团队只读
const OWNER_TEAM = '查询编排团队'

type Row = Record<string, any>
type RunRecord = Record<string, any>

function asText(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

export default function FlowView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [team, setTeam] = useState(OWNER_TEAM)
  const [activeRow, setActiveRow] = useState<Row | null>(null)
  const [runs, setRuns] = useState<RunRecord[]>([])
  const [detail, setDetail] = useState<RunRecord | null>(null)
  const [contracts, setContracts] = useState<Row[]>([])
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('编排流程列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '查询编排列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message || '查询编排动作未生效，请稍后重试')
      }
      setNotice(payload.message)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '查询编排操作失败')
    }
  }

  async function executeFlow(row: Row, failSteps: string[] = []) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/execute`, {
        method: 'POST',
        body: JSON.stringify({ team, fail_steps: failSteps }),
      })
      const payload = await response.json()
      if (!response.ok) {
        throw new Error(payload.message || '编排执行请求失败')
      }
      setNotice(payload.message)
      await reload()
      if (activeRow && String(activeRow.id) === String(row.id)) {
        await openRuns(row)
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '编排执行失败')
    }
  }

  async function reorder(row: Row, orderedStepIds: string[]) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/steps/reorder`, {
        method: 'POST',
        body: JSON.stringify({ team, ordered_step_ids: orderedStepIds }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message || '步骤顺序调整被拒绝')
      }
      setNotice(payload.message)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '步骤顺序调整失败')
    }
  }

  async function openRuns(row: Row) {
    setActiveRow(row)
    setDetail(null)
    setContracts([])
    try {
      const [runsResp, contractResp] = await Promise.all([
        request(`${ENDPOINT}/${row.id}/runs`),
        request(`${ENDPOINT}/contracts/list?flow_no=${encodeURIComponent(row['流程编号'])}`),
      ])
      const runsPayload = await runsResp.json()
      const contractPayload = await contractResp.json()
      setRuns(runsPayload.items ?? [])
      setContracts(contractPayload.items ?? [])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '执行记录读取失败')
    }
  }

  async function openRunDetail(runNo: number) {
    if (!activeRow) return
    try {
      const response = await request(`${ENDPOINT}/${activeRow.id}/runs/${runNo}`)
      if (!response.ok) throw new Error('执行批次明细读取失败')
      setDetail(await response.json())
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '执行批次明细读取失败')
    }
  }

  const isOwner = team === OWNER_TEAM

  return (
    <section className="page" data-module="flow">
      <header className="page-head">
        <div>
          <h2>查询编排管理</h2>
          <p className="page-desc">按数据依赖执行步骤、失败回退并合并结果；执行顺序与步骤记录可逐项复核，并写入编排步骤契约供转换模块只读消费。</p>
        </div>
        <div className="page-actions">
          <label className="team-switch">
            当前团队：
            <select value={team} onChange={(event) => setTeam(event.target.value)}>
              <option value={OWNER_TEAM}>{OWNER_TEAM}（可执行/改顺序）</option>
              <option value="分析团队">分析团队（只读）</option>
              <option value="消费方团队">消费方团队（只读）</option>
            </select>
          </label>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出查询编排清单
          </button>
        </div>
      </header>

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

      {notice && <div className="notice ok-text">{notice}</div>}
      {errorMessage && <div className="notice error-text">{errorMessage}</div>}

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
              {columns.map((column) => <td key={column}>{asText(row[column])}</td>)}
              <td className="row-actions flow-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
                <button
                  className="link primary-link"
                  type="button"
                  disabled={!isOwner}
                  title={isOwner ? '按数据依赖执行编排' : '仅归属团队可执行'}
                  onClick={() => void executeFlow(row)}
                >
                  执行编排
                </button>
                <button
                  className="link"
                  type="button"
                  disabled={!isOwner}
                  title={isOwner ? '注入失败复测回退' : '仅归属团队可执行'}
                  onClick={() => void executeFlow(row, ['B'])}
                >
                  失败回退复测
                </button>
                <button className="link" type="button" onClick={() => void openRuns(row)}>
                  执行记录/契约
                </button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无查询编排数据，可先登记编排流程</td>
            </tr>
          )}
        </tbody>
      </table>

      {activeRow && (
        <div className="drawer">
          <div className="drawer-head">
            <h3>{activeRow['流程编号']} · 执行复核（共享执行结果只读，仅归属团队可改顺序）</h3>
            <button className="btn ghost" type="button" onClick={() => { setActiveRow(null); setDetail(null) }}>关闭</button>
          </div>

          {isOwner && (
            <div className="drawer-toolbar">
              <button className="btn" type="button" onClick={() => void reorder(activeRow, ['A', 'C', 'B'])}>
                同波次互换 B↔C
              </button>
              <button className="btn" type="button" onClick={() => void reorder(activeRow, ['B', 'C', 'A'])}>
                跨波次调整（应被拒）
              </button>
            </div>
          )}

          <h4>执行批次（{runs.length}）</h4>
          <table className="data-table compact">
            <thead>
              <tr><th>批次号</th><th>批次状态</th><th>执行顺序</th><th>并行度</th><th>回退次数</th><th>一致性闸门</th><th>操作</th></tr>
            </thead>
            <tbody>
              {runs.map((run) => (
                <tr key={String(run['执行批次号'])}>
                  <td>{run['执行批次号']}</td>
                  <td>{run['批次状态']}</td>
                  <td>{(run['执行顺序'] ?? []).join(' → ')}</td>
                  <td>{run['并行度']}</td>
                  <td>{(run['失败回退记录'] ?? []).length}</td>
                  <td>{run['一致性闸门']?.通过 ? '通过' : '未通过'}</td>
                  <td>
                    <button className="link" type="button" onClick={() => void openRunDetail(run['执行批次号'])}>
                      逐项复核
                    </button>
                  </td>
                </tr>
              ))}
              {runs.length === 0 && (
                <tr><td className="empty-state" colSpan={7}>该编排尚无执行批次</td></tr>
              )}
            </tbody>
          </table>

          <h4>编排步骤契约（转换模块读取同一份，只读）</h4>
          <table className="data-table compact">
            <thead>
              <tr><th>契约版本</th><th>来源批次</th><th>执行顺序</th><th>字段数</th><th>合并行数</th><th>写入团队</th><th>写入时间</th></tr>
            </thead>
            <tbody>
              {contracts.map((contract) => (
                <tr key={String(contract.id)}>
                  <td>v{contract['契约版本']}</td>
                  <td>#{contract['来源执行批次']}</td>
                  <td>{(contract['执行顺序'] ?? []).join(' → ')}</td>
                  <td>{(contract['字段清单'] ?? []).length}</td>
                  <td>{contract['合并行数']}</td>
                  <td>{contract['写入团队']}</td>
                  <td>{contract['写入时间']}</td>
                </tr>
              ))}
              {contracts.length === 0 && (
                <tr><td className="empty-state" colSpan={7}>尚无契约：执行成功并通过一致性闸门后才会写入</td></tr>
              )}
            </tbody>
          </table>

          {detail && (
            <div className="run-detail">
              <h4>批次 #{detail['执行批次号']} 逐项复核</h4>
              <p className="page-desc">
                状态：{detail['批次状态']} ｜ 执行顺序：{(detail['执行顺序'] ?? []).join(' → ')} ｜
                拓扑波次：{JSON.stringify(detail['拓扑波次'])}
              </p>

              <table className="data-table compact">
                <thead>
                  <tr><th>步骤</th><th>波次</th><th>前置依赖</th><th>状态</th><th>尝试记录</th><th>产物字段</th></tr>
                </thead>
                <tbody>
                  {(detail['步骤执行记录'] ?? []).map((record: Row) => (
                    <tr key={record['步骤编号']}>
                      <td>{record['步骤编号']}</td>
                      <td>{record['波次']}</td>
                      <td>{(record['前置依赖'] ?? []).join('、') || '—'}</td>
                      <td>{record['状态']}</td>
                      <td>
                        {(record['尝试记录'] ?? []).map((attempt: Row, index: number) => (
                          <span key={index} className="attempt-chip">
                            第{attempt['尝试']}次:{attempt['结果']}{attempt['有效性'] === '已作废' ? '(已作废)' : ''}
                          </span>
                        ))}
                      </td>
                      <td>{(record['产物字段'] ?? []).join('、') || '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>

              {(detail['失败回退记录'] ?? []).length > 0 && (
                <>
                  <h4>失败回退记录</h4>
                  {(detail['失败回退记录'] ?? []).map((fb: Row, index: number) => (
                    <div key={index} className="fallback-box">
                      失败步骤 {fb['失败步骤']} → 回退边界 {fb['回退边界']} ｜
                      受影响步骤：{(fb['受影响步骤'] ?? []).join('、')} ｜
                      依据：{fb['回退依据']}
                    </div>
                  ))}
                </>
              )}

              {detail['合并结果'] && (
                <>
                  <h4>合并数据集（{detail['合并结果'].row_count} 行，可回溯到步骤）</h4>
                  <table className="data-table compact">
                    <thead>
                      <tr>
                        {Object.keys((detail['合并结果'].merged_rows ?? [])[0] ?? {}).map((key) => <th key={key}>{key}</th>)}
                      </tr>
                    </thead>
                    <tbody>
                      {(detail['合并结果'].merged_rows ?? []).map((row: Row, index: number) => (
                        <tr key={index}>
                          {Object.values(row).map((value, cellIndex) => <td key={cellIndex}>{asText(value)}</td>)}
                        </tr>
                      ))}
                    </tbody>
                  </table>

                  <h4>字段清单与口径优先级判定依据</h4>
                  <table className="data-table compact">
                    <thead>
                      <tr><th>字段</th><th>采纳来源</th><th>全部来源</th><th>判定依据</th></tr>
                    </thead>
                    <tbody>
                      {(detail['合并结果'].field_inventory ?? []).map((item: Row) => (
                        <tr key={item['字段']}>
                          <td>{item['字段']}</td>
                          <td>{item['采纳来源步骤']}</td>
                          <td>{(item['全部来源步骤'] ?? []).join('、')}</td>
                          <td>{item['判定依据']}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </>
              )}
            </div>
          )}
        </div>
      )}

      <footer className="page-foot">
        <span>共 {total} 条查询编排记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
