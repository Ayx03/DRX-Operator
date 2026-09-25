import { useState, useEffect, useCallback, useRef } from 'react';

/* ================================================================
   ReplayControls — step-through / auto-play timeline controls
   ================================================================ */

const SPEED_OPTIONS = [0.5, 1, 2, 4, 8];

export default function ReplayControls({
  totalSteps,
  currentStep,
  onStepChange,
  isPlaying,
  onTogglePlay,
  onReset,
  timelineEvents = [],
}) {
  const [speed, setSpeed] = useState(1);
  const intervalRef = useRef(null);

  // Auto-play logic
  useEffect(() => {
    if (isPlaying && currentStep < totalSteps - 1) {
      intervalRef.current = setInterval(() => {
        onStepChange((prev) => {
          const next = prev + 1;
          if (next >= totalSteps) {
            onTogglePlay(false);
            return prev;
          }
          return next;
        });
      }, 1200 / speed);
    } else {
      clearInterval(intervalRef.current);
    }
    return () => clearInterval(intervalRef.current);
  }, [isPlaying, speed, totalSteps, currentStep, onStepChange, onTogglePlay]);

  const cycleSpeed = useCallback(() => {
    setSpeed((prev) => {
      const idx = SPEED_OPTIONS.indexOf(prev);
      return SPEED_OPTIONS[(idx + 1) % SPEED_OPTIONS.length];
    });
  }, []);

  const handleProgressClick = useCallback((e) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const ratio = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    const step = Math.round(ratio * (totalSteps - 1));
    onStepChange(step);
  }, [totalSteps, onStepChange]);

  const progress = totalSteps > 1 ? (currentStep / (totalSteps - 1)) * 100 : 0;
  const currentEvent = timelineEvents[currentStep] || {};

  if (totalSteps === 0) return null;

  return (
    <div className="replay-controls">
      {/* Reset */}
      <button className="replay-btn secondary" onClick={onReset} title="Reset">
        ⏮
      </button>

      {/* Step back */}
      <button
        className="replay-btn secondary"
        onClick={() => onStepChange(Math.max(0, currentStep - 1))}
        disabled={currentStep === 0}
        title="Step back"
      >
        ⏪
      </button>

      {/* Play/Pause */}
      <button className="replay-btn primary" onClick={() => onTogglePlay(!isPlaying)} title={isPlaying ? 'Pause' : 'Play'}>
        {isPlaying ? '⏸' : '▶'}
      </button>

      {/* Step forward */}
      <button
        className="replay-btn secondary"
        onClick={() => onStepChange(Math.min(totalSteps - 1, currentStep + 1))}
        disabled={currentStep >= totalSteps - 1}
        title="Step forward"
      >
        ⏩
      </button>

      {/* Progress bar */}
      <div className="replay-progress">
        <div className="replay-progress-bar" onClick={handleProgressClick}>
          <div className="replay-progress-fill" style={{ width: `${progress}%` }} />
        </div>
        <div className="replay-info">
          <span>Step {currentStep + 1} / {totalSteps}</span>
          <span>{currentEvent.label || ''}</span>
        </div>
      </div>

      {/* Speed control */}
      <button className="replay-speed" onClick={cycleSpeed} title="Change speed">
        {speed}x
      </button>
    </div>
  );
}
