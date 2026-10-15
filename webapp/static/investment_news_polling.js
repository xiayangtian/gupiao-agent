/* Time-window helpers for low-noise investment-news background refresh. */
(function (root) {
  'use strict';

  var SCHEDULED_HOURS = [9, 10, 11, 12, 13, 14, 15, 16];
  var BUFFER_MINUTES = 5;

  function shanghaiMinuteOfDay(date) {
    if (!(date instanceof Date) || Number.isNaN(date.getTime())) return null;
    var parts = new Intl.DateTimeFormat('en-GB', {
      timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
    }).formatToParts(date);
    var values = {};
    parts.forEach(function (part) { values[part.type] = part.value; });
    return Number(values.hour) * 60 + Number(values.minute);
  }

  function isWithinScheduleWindow(date) {
    var minuteOfDay = shanghaiMinuteOfDay(date || new Date());
    if (minuteOfDay === null) return false;
    return SCHEDULED_HOURS.some(function (hour) {
      return Math.abs(minuteOfDay - hour * 60) <= BUFFER_MINUTES;
    });
  }

  root.InvestmentNewsPolling = Object.freeze({
    isWithinScheduleWindow: isWithinScheduleWindow,
    intervalMs: 30000,
  });
})(window);
