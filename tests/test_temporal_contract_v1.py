"""Cross-component temporal contract regression tests (pure stdlib)."""
import importlib.util
import pathlib
import unittest

MODULE = pathlib.Path(__file__).resolve().parents[1] / "contracts" / "temporal_contract_v1.py"
spec = importlib.util.spec_from_file_location("temporal_contract_v1", MODULE)
contract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(contract)

class TemporalContractV1Tests(unittest.TestCase):
    def test_all_contexts(self):
        for family in ("c1","c2","c3","c4","c5","c6","c7","c8"):
            profile=contract.profile("vr1",family)
            self.assertIn(profile[1],profile[0])
            self.assertEqual(contract.resolve("vr1",family)["contractVersion"],"1.0.0")
        for vr in ("vr2","vr3","vr4","vr5"):
            self.assertIn(contract.resolve(vr,"default")["sourceTimeframe"],contract.BASE_CANDLE_TIMEFRAMES)

    def test_daily_windows_are_not_candle_intervals(self):
        for family, choices in (("c4",("7D","30D")),("c5",("7D","30D","90D"))):
            for choice in choices:
                result=contract.resolve("vr1",family,choice)
                self.assertEqual(result["sourceTimeframe"],"1D")
                self.assertEqual(result["kind"],"WINDOW")
                self.assertEqual(result["windowDays"],int(choice[:-1]))
                self.assertEqual(contract.validate_selection(result),result)

    def test_rejects_invalid_choices_and_contracts(self):
        with self.assertRaises(ValueError): contract.resolve("vr3","default","1m")
        with self.assertRaises(ValueError): contract.resolve("vr1","c4","1m")
        with self.assertRaises(ValueError): contract.profile("vr1","c9")
        value=contract.resolve("vr1","c5","30D")
        for broken in ({**value,"contractVersion":"2.0.0"},
                       {**value,"sourceTimeframe":"30D"},
                       {**value,"windowDays":7}):
            with self.assertRaises(ValueError): contract.validate_selection(broken)

    def test_funding_and_etf_timings_remain_independent(self):
        etf=contract.resolve("vr1","c4","7D")
        self.assertEqual(etf["etfObservationCadence"],"SESSION")
        self.assertEqual(etf["exchangeFlowCadence"],"1H")
        oi=contract.resolve("vr1","c3","1H")
        self.assertIsNone(oi["etfObservationCadence"])

if __name__=="__main__":
    unittest.main()
