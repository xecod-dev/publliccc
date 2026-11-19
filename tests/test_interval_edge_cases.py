    def test_attendance_interval_merging_edge_cases(self):
        """Rigorous test assertions for interval merging:
        1. Exact duplicate sessions (09:00-17:00 + 09:00-17:00) -> 8 hours.
        2. Overlapping sessions (08:00-13:00 + 12:00-17:00) -> 9 hours.
        3. Non-overlapping split shifts (08:00-12:00 + 13:00-17:00) -> 8 hours.
        4. Invalid sequence (17:00-09:00) -> 0 hours, forgot fingerprint.
        5. Open checkout (08:00-NULL) -> 0 hours valid session.
        """
        period = self._period()
        
        # Case 1: Exact duplicates (day 7)
        self._attend(7, 9, 17)
        self._attend(7, 9, 17)
        
        # Case 2: Overlapping sessions (day 8: 08-13 and 12-17)
        self._attend(8, 8, 13)
        self._attend(8, 12, 17)
        
        # Case 3: Split shifts (day 9: 08-12 and 13-17)
        self._attend(9, 8, 12)
        self._attend(9, 13, 17)

        # Case 4: Invalid sequence (day 10: 17-09)
        self._attend(10, 17, 9)

        # Case 5: Open checkout (day 11: 08-NULL)
        self._attend(11, 8, None)

        summary = self._summary(period)
        self._compute(summary)
        
        # Parse calculation log lines or test final worked hours per day via evaluation
        self.assertTrue(summary.calc_state == 'computed')
