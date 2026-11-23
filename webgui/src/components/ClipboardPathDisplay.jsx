import { FILE_PATH_DISPLAYS, normalizePathDisplay } from '../clipboardFormat.js'

// Chooses how file cards name their files: source folder, file name or
// relative path. The choice is saved in the FlowShift settings.
export default function ClipboardPathDisplay({ value, onChange, className = '' }) {
  return (
    <select
      className={`clipboard-path-display ${className}`}
      aria-label="Show files as"
      title="Show files as folder, file name or relative path"
      value={normalizePathDisplay(value)}
      onChange={(event) => onChange(normalizePathDisplay(event.target.value))}
    >
      {FILE_PATH_DISPLAYS.map((option) => (
        <option key={option.value} value={option.value}>{option.short}</option>
      ))}
    </select>
  )
}
