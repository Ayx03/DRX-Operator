/* ================================================================
   Detail Panel — shows full info for a selected node
   ================================================================ */

export default function DetailPanel({ node, onClose }) {
  if (!node) return null;

  const data = node.data || {};
  const ts = data.timestamp
    ? new Date(data.timestamp * 1000).toLocaleString('zh-CN')
    : '';

  return (
    <div className="detail-panel animate-slide-in">
      <div className="detail-header">
        <div className="detail-title">
          <span style={{
            width: 10, height: 10, borderRadius: '50%',
            background: data.color || '#6b7280', display: 'inline-block'
          }} />
          {data.label || node.type || 'Node'}
        </div>
        <button className="detail-close" onClick={onClose}>✕</button>
      </div>

      <div className="detail-body">
        {/* Metadata */}
        <div className="detail-section">
          <div className="detail-section-title">Metadata</div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 8 }}>
            {data.category && (
              <span className="detail-badge" style={{
                background: `${data.color || '#6b7280'}22`,
                color: data.color || '#6b7280',
                border: `1px solid ${data.color || '#6b7280'}44`,
              }}>
                {data.category}
              </span>
            )}
            {data.status && (
              <span className={`detail-badge rf-node-status ${data.status}`}>
                {data.status}
              </span>
            )}
            {data.actor && (
              <span className="detail-badge" style={{
                background: 'rgba(6, 182, 212, 0.12)',
                color: 'var(--accent-cyan)',
                border: '1px solid rgba(6, 182, 212, 0.25)',
              }}>
                {data.actor}
              </span>
            )}
          </div>
          {ts && (
            <div style={{ fontSize: 11, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>
              {ts}
            </div>
          )}
        </div>

        {/* Text content */}
        {(data.fullText || data.text) && (
          <div className="detail-section">
            <div className="detail-section-title">Content</div>
            <div className="detail-code">
              {data.fullText || data.text}
            </div>
          </div>
        )}

        {/* Tool input */}
        {(data.fullInput || data.input) && (
          <div className="detail-section">
            <div className="detail-section-title">Tool Input</div>
            <div className="detail-code">
              {data.fullInput || data.input}
            </div>
          </div>
        )}

        {/* Tool output */}
        {(data.fullOutput || data.output) && (
          <div className="detail-section">
            <div className="detail-section-title">Tool Output</div>
            <div className="detail-code">
              {data.fullOutput || data.output}
            </div>
          </div>
        )}

        {/* Approval details */}
        {data.details && typeof data.details === 'object' && (
          <div className="detail-section">
            <div className="detail-section-title">Details</div>
            <div className="detail-code">
              {JSON.stringify(data.details, null, 2)}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
