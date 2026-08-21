export interface SourceLink {
  label: string
  url: string
}

interface Props {
  text: string
  links?: SourceLink[]
}

/** Small "?" icon with a hover tooltip explaining the data source, with web links. */
export default function SourceHint({ text, links }: Props) {
  return (
    <span className="source-hint">
      ?
      <span className="source-hint-pop">
        <span className="source-hint-text">{text}</span>
        {links && links.length > 0 && (
          <span className="source-hint-links">
            {links.map((l) => (
              <a
                key={l.url}
                className="source-hint-link"
                href={l.url}
                target="_blank"
                rel="noreferrer"
              >
                {l.label} ↗
              </a>
            ))}
          </span>
        )}
      </span>
    </span>
  )
}
