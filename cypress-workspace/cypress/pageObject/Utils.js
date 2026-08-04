export function calculateShiftMinutes(startTime, endTime) {
    const [startHour, startMinute] = startTime.split(':').map(Number)
    const [endHour, endMinute] = endTime.split(':').map(Number)

    const startTotalMinutes = startHour * 60 + startMinute;
    const endTotalMinutes = endHour * 60 + endMinute;

    return endTotalMinutes - startTotalMinutes;
}

export function calculateTotalDays(startDateStr, endDateStr) {
    const startDate = new Date(startDateStr);
    const endDate = new Date(endDateStr);

    // Difference in milliseconds
    const diffTime = Math.abs(endDate - startDate);

    // Convert to days
    const diffDays = Math.ceil(diffTime / (1000 * 60 * 60 * 24)) + 1;

    return diffDays;
}

export function getFormattedTodayDate() {
    const today = new Date();
  
    const day = String(today.getDate()).padStart(2, '0');
    const monthIndex = today.getMonth();
    const year = today.getFullYear();
  
    const monthNames = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    const monthShort = monthNames[monthIndex];
  
    return `${day}/${monthShort}/${year}`;
  }
  
 export function calculateExperience(fromMonth, fromYear, toMonth, toYear) {
  const monthIndex = {
    January: 0, February: 1, March: 2, April: 3, May: 4, June: 5,
    July: 6, August: 7, September: 8, October: 9, November: 10, December: 11
  };

  const fromDate = new Date(fromYear, monthIndex[fromMonth]);
  const toDate = new Date(toYear, monthIndex[toMonth]);

  const diffInMonths = (toDate.getFullYear() - fromDate.getFullYear()) * 12 +
                       (toDate.getMonth() - fromDate.getMonth());

  const years = Math.floor(diffInMonths / 12);
  const months = diffInMonths % 12;

  return parseFloat((years + months / 100).toFixed(2)); // Now 1 year 1 month = 1.01
}


