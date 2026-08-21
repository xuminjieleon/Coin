import { useEffect, useState } from 'react'
import { fetchCalendar, type CalendarResponse } from '../api/client'
import SourceHint from './SourceHint'

const IMPACT_CLASS: Record<string, string> = {
  high: 'cal-high',
  medium: 'cal-medium',
  low: 'cal-low',
}

export default function CalendarPanel() {
  const [cal, setCal] = useState<CalendarResponse | null>(null)

  useEffect(() => {
    let cancelled = false
    fetchCalendar()
      .then((c) => {
        if (!cancelled) setCal(c)
      })
      .catch(() => {
        /* ignore */
      })
    return () => {
      cancelled = true
    }
  }, [])

  const events = cal?.events ?? []
  return (
    <div className="panel">
      <div className="panel-title-row">
        <div className="panel-title">事件日历</div>
        <SourceHint
          text={cal?.note ?? '宏观事件与代币解锁日历。结构分析的前提假设是无外生冲击，重大事件前后请降低信号权重。'}
          links={[{ label: '美联储官网（FOMC 日程）', url: 'https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm' }]}
        />
      </div>
      {events.length === 0 && <div className="panel-empty">暂无近期事件</div>}
      <div className="cal-list">
        {events.map((e) => (
          <div className="cal-item" key={`${e.date}-${e.title}`}>
            <span className="cal-date">
              {e.date.slice(5).replace('-', '/')}
              {e.time ? ` ${e.time}` : ''}
            </span>
            <span className={`cal-impact ${IMPACT_CLASS[e.impact] ?? 'cal-low'}`}>
              {e.impact === 'high' ? '高' : e.impact === 'medium' ? '中' : '低'}
            </span>
            <span className="cal-title" title={e.title}>
              {e.title}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}
