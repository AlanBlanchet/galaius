import 'package:flutter/material.dart';

void main() => runApp(const App());

class App extends StatelessWidget {
  const App({super.key});
  @override
  Widget build(BuildContext c) => MaterialApp(
        debugShowCheckedModeBanner: false,
        home: Scaffold(
          // No animation anywhere: every frame is identical unless something actually scrolled.
          body: Stack(children: [
            ListView.builder(
              itemCount: 60,
              itemExtent: 56,
              itemBuilder: (_, i) => Container(
                color: i.isEven ? Colors.white : const Color(0xFFEEEEEE),
                alignment: Alignment.centerLeft,
                padding: const EdgeInsets.only(left: 16),
                child: Text('ROW $i', style: const TextStyle(fontSize: 24, color: Colors.black)),
              ),
            ),
            DraggableScrollableSheet(
              initialChildSize: 0.25,
              minChildSize: 0.25,
              maxChildSize: 0.9,
              builder: (_, ctrl) => Container(
                color: const Color(0xFF1565C0),
                child: ListView.builder(
                  controller: ctrl,
                  itemCount: 40,
                  itemExtent: 48,
                  itemBuilder: (_, i) => Padding(
                    padding: const EdgeInsets.only(left: 16),
                    child: Text('SHEET $i',
                        style: const TextStyle(fontSize: 20, color: Colors.white)),
                  ),
                ),
              ),
            ),
          ]),
        ),
      );
}
