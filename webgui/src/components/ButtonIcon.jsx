// Inline icons for buttons whose meaning rests on the icon. Font Awesome comes
// from a CDN; when that stylesheet does not load (offline, blocked) an <i>
// icon renders as nothing, so icon-led buttons draw their symbol here.
const ICON_PATHS = {
  'arrow-up': <path d="M8 13V3M3.8 7.2L8 3l4.2 4.2" />,
  'arrow-down': <path d="M8 3v10M3.8 8.8L8 13l4.2-4.2" />,
  xmark: <path d="M4 4l8 8M12 4l-8 8" />,
  download: <path d="M8 2.5v7.5M4.8 6.8L8 10l3.2-3.2M2.5 11v2.5h11V11" />,
  install: <path d="M8 2v6M4.6 4.3a5 5 0 1 0 6.8 0" />,
  save: <><path d="M3 2.5h8l2.5 2.5v8.5h-11z" /><path d="M5.5 2.5v3.5h5V2.5M5 13.5v-4h6v4" /></>,
  trash: <path d="M2.5 4.5h11M6.3 4.5V2.8h3.4v1.7M4.2 4.5l.7 8.7h6.2l.7-8.7M6.8 7v4M9.2 7v4" />,
}

export default function ButtonIcon({ name, size = 14 }) {
  return (
    <svg className="button-icon" data-icon={name} viewBox="0 0 16 16" width={size} height={size} aria-hidden="true"
      focusable="false" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
      {ICON_PATHS[name]}
    </svg>
  )
}
