import { Component, useState, useCallback } from 'react'

class _ErrorBoundaryInner extends Component {
  constructor(props) {
    super(props)
    this.state = { error: null }
  }
  static getDerivedStateFromError(error) {
    return { error }
  }
  render() {
    if (this.state.error) {
      return this.props.renderError(this.state.error, () => this.setState({ error: null }))
    }
    return this.props.children
  }
}

export default function ErrorBoundary({ children, name }) {
  const [key, setKey] = useState(0)

  const renderError = useCallback((error, reset) => (
    <div className="error-banner" style={{ margin: 0 }}>
      <i className="fas fa-bug" />
      <div>
        <strong>Error in {name || 'component'}:</strong> {error.message}
        <button
          className="btn btn-ghost btn-sm"
          style={{ marginLeft: 12 }}
          onClick={() => { reset(); setKey((k) => k + 1) }}
        >
          <i className="fas fa-rotate" /> Retry
        </button>
      </div>
    </div>
  ), [name])

  return (
    <_ErrorBoundaryInner key={key} renderError={renderError}>
      {children}
    </_ErrorBoundaryInner>
  )
}
